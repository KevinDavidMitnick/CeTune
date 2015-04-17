import os,sys
lib_path = os.path.abspath(os.path.join('../conf/'))
sys.path.append(lib_path)
import common
import time
import pprint
import re
import socket
import uuid
import argparse

pp = pprint.PrettyPrinter(indent=4)
class Deploy:
    def __init__(self):
        self.all_conf_data = common.Config("../conf/all.conf")
        self.cluster = {}
        self.cluster["user"] = self.all_conf_data.get("user")
        self.cluster["head"] = self.all_conf_data.get("head")
        self.cluster["clients"] = self.all_conf_data.get_list("list_client")
        self.cluster["osds"] = {}
        for osd in self.all_conf_data.get_list("list_ceph"):
            self.cluster["osds"][osd] = socket.gethostbyname(osd)

        self.cluster["mons"] = {}
        for mon in self.all_conf_data.get_list("list_mon"): 
            self.cluster["mons"][mon] = socket.gethostbyname(mon)

        for osd in self.cluster["osds"]:
            self.cluster[osd] = self.all_conf_data.get_list(osd)

        self.cluster["fs"] = "xfs"        
        self.cluster["mkfs_opts"] = "-f -i size=2048 -n size=64k"        
        self.cluster["mount_opts"] = "-o inode64,noatime,logbsize=256k"
        
        self.cluster["ceph_conf"] = {}
        self.cluster["ceph_conf"]["global"] = {}
        self.cluster["ceph_conf"]["global"]["auth_service_required"] = "none"
        self.cluster["ceph_conf"]["global"]["auth_cluster_required"] = "none"
        self.cluster["ceph_conf"]["global"]["auth_client_required"] = "none"
        self.cluster["ceph_conf"]["global"]["mon_data"] = "/var/lib/ceph/mon.$id"
        self.cluster["ceph_conf"]["global"]["osd_data"] = "/var/lib/ceph/mnt/osd-device-$id-data"
        if self.all_conf_data.get("ceph_conf"):
            for key, value in self.all_conf_data.get("ceph_conf").items():
                self.cluster["ceph_conf"]["global"][key] = value

        self.cluster["ceph_conf"]["client"] = {}
        self.cluster["ceph_conf"]["client"]["rbd_cache"] = "false"

    def gen_cephconf(self):
        cephconf = []
        for section in self.cluster["ceph_conf"]:
            cephconf.append("[%s]\n" % section)
            for key, value in self.cluster["ceph_conf"][section].items():
                cephconf.append("    %s = %s\n" % (key, value))
        for mon in self.cluster["mons"]:
            cephconf.append("[mon.%s]\n" % mon)
            cephconf.append("    host = %s\n" % mon)
            cephconf.append("    mon addr = %s\n" % self.cluster["mons"][mon])
        osd_id = 0
        for osd in sorted(self.cluster["osds"]):
            for device_bundle in common.get_list(self.cluster[osd]):
                osd_device = device_bundle[0]
                journal_device = device_bundle[1]
                cephconf.append("[osd.%d]\n" % osd_id)
                osd_id += 1
                cephconf.append("    host = %s\n" % osd)
                cephconf.append("    public addr = %s\n" % self.cluster["osds"][osd])
                cephconf.append("    cluster addr = %s\n" % self.cluster["osds"][osd])
                cephconf.append("    osd journal = %s\n" % osd_device)
                cephconf.append("    devs = %s\n" % journal_device)
        output = "".join(cephconf)
        with open("../conf/ceph.conf", 'w') as f:
            f.write(output)

    def redeploy(self):
        self.gen_cephconf()
        print common.bcolors.OKGREEN + "[LOG]ceph.conf file generated" +common.bcolors.ENDC
        self.cleanup()
        print common.bcolors.OKGREEN + "[LOG]Killed ceph-mon, ceph-osd and cleaned mon dir" +common.bcolors.ENDC

        print common.bcolors.OKGREEN + "[LOG]Started to mkfs.xfs on osd devices" +common.bcolors.ENDC
        self.make_osd_fs()
        print common.bcolors.OKGREEN + "[LOG]Succeded in mkfs.xfs on osd devices" +common.bcolors.ENDC
        self.distribute_conf()
        print common.bcolors.OKGREEN + "[LOG]ceph.conf Distributed to all nodes" +common.bcolors.ENDC

        print common.bcolors.OKGREEN + "[LOG]Started to build mon daemon" +common.bcolors.ENDC
        self.make_mon()        
        print common.bcolors.OKGREEN + "[LOG]Succeeded in building mon daemon" +common.bcolors.ENDC
        print common.bcolors.OKGREEN + "[LOG]Started to build osd daemon" +common.bcolors.ENDC
        self.make_osd()
        print common.bcolors.OKGREEN + "[LOG]Succeeded in building osd daemon" +common.bcolors.ENDC

    def shutdown(self):
        pass

    def cleanup(self):
        user = self.cluster["user"]
        mons = self.cluster["mons"]
        osds = self.cluster["osds"]
        mon_basedir = os.path.dirname(self.cluster["ceph_conf"]["global"]["mon_data"])
        mon_filename = os.path.basename(self.cluster["ceph_conf"]["global"]["mon_data"]).replace("$id","*")
        common.pdsh( user, mons, "sudo killall -9 ceph-mon", option="check_return")
        common.pdsh( user, osds, "sudo killall -9 ceph-osd", option="check_return")

    def distribute_conf(self):
        user = self.cluster["user"]
        clients = self.cluster["clients"]
        osds = sorted(self.cluster["osds"])

        for client in clients:
            common.scp(user, client, "../conf/ceph.conf", "/etc/ceph/")
        for osd in osds:
            common.scp(user, osd, "../conf/ceph.conf", "/etc/ceph/")

    def make_osd_fs(self):
        user = self.cluster["user"]
        osds = sorted(self.cluster["osds"])
        mkfs_opts = self.cluster['mkfs_opts']
        mount_opts = self.cluster['mount_opts']
        osd_basedir = os.path.dirname(self.cluster["ceph_conf"]["global"]["osd_data"])
        osd_filename = os.path.basename(self.cluster["ceph_conf"]["global"]["osd_data"])
        
        self.cluster["ceph_conf"]["global"]["osd_data"]
        stdout, stderr = common.pdsh( user, osds, 'mount -l', option="check_return" )
        mount_list = {}
        for node, mount_list_tmp in common.format_pdsh_return(stdout).items():
            mount_list[node] = {}
            for line in mount_list_tmp.split('\n'):
                tmp = line.split()
                mount_list[node][tmp[0]] = tmp[2]

        osd_num = 0
        for osd in osds:
            for device_bundle in common.get_list(self.cluster[osd]):
                osd_device = device_bundle[0]
                journal_device = device_bundle[1]
                print common.bcolors.OKGREEN + "[LOG]mkfs.xfs for %s on %s" % (osd_device, osd) +common.bcolors.ENDC
                try:
                    mounted_dir = mount_list[osd][osd_device]
                    common.pdsh( user, [osd], 'umount %s' % osd_device )
                    common.pdsh( user, [osd], 'rm -rf %s' % mounted_dir )
                except:
                    pass
                common.pdsh( user, [osd], 'mkfs.xfs %s %s' % (mkfs_opts, osd_device))
                osd_filedir = osd_filename.replace("$id", str(osd_num))
                common.pdsh( user, [osd], 'mkdir -p %s/%s' % (osd_basedir, osd_filedir))
                common.pdsh( user, [osd], 'mount %s -t xfs %s %s/%s' % (mount_opts, osd_device, osd_basedir, osd_filedir))
                osd_num += 1

    def make_osd(self):
        user = self.cluster["user"]
        osds = sorted(self.cluster["osds"])
        mons = self.cluster["mons"]
        mon_basedir = os.path.dirname(self.cluster["ceph_conf"]["global"]["mon_data"])
        osd_basedir = os.path.dirname(self.cluster["ceph_conf"]["global"]["osd_data"])
        osd_filename = os.path.basename(self.cluster["ceph_conf"]["global"]["osd_data"])
        osd_num = 0

        for osd in osds:
            for device_bundle in common.get_list(self.cluster[osd]):
                osd_device = device_bundle[0]
                journal_device = device_bundle[1]

                # Build the OSD
                osduuid = str(uuid.uuid4())
                osd_filedir = osd_filename.replace("$id",str(osd_num))
                key_fn = '%s/%s/keyring' % (osd_basedir, osd_filedir)
                common.pdsh(user, [osd], 'ceph osd create %s' % (osduuid))
                common.pdsh(user, [osd], 'ceph osd crush add osd.%d 1.0 host=%s rack=localrack root=default' % (osd_num, osd), option="check_return")
                common.pdsh(user, [osd], 'sh -c "ulimit -n 16384 && ulimit -c unlimited && exec ceph-osd -i %d --mkfs --mkkey --osd-uuid %s"' % (osd_num, osduuid), option="check_return")
                common.pdsh(user, [osd], 'ceph -i %s/keyring auth add osd.%d osd "allow *" mon "allow profile osd"' % (mon_basedir, osd_num), option="check_return")

                # Start the OSD
                common.pdsh(user, [osd], 'mkdir -p %s/pid' % mon_basedir)
                pidfile="%s/pid/ceph-osd.%d.pid" % (mon_basedir, osd_num)
                cmd = 'ceph-osd -i %d --pid-file=%s' % (osd_num, pidfile)
                cmd = 'ceph-run %s' % cmd
                common.pdsh(user, [osd], 'sudo sh -c "ulimit -n 16384 && ulimit -c unlimited && exec %s"' % cmd, option="check_return")
                print common.bcolors.OKGREEN + "[LOG]Builded osd.%s daemon on %s" % (osd_num, osd) +common.bcolors.ENDC
                osd_num = osd_num+1

    def make_mon(self):
        user = self.cluster["user"]
        osds = sorted(self.cluster["osds"])
        mons = self.cluster["mons"]
        mon_basedir = os.path.dirname(self.cluster["ceph_conf"]["global"]["mon_data"])
        # Keyring
        mon = mons.keys()[0]
        common.pdsh(user, [mon], 'ceph-authtool --create-keyring --gen-key --name=mon. %s/keyring --cap mon \'allow *\'' % mon_basedir)
        common.pdsh(user, [mon], 'ceph-authtool --gen-key --name=client.admin --set-uid=0 --cap mon \'allow *\' --cap osd \'allow *\' --cap mds allow %s/keyring' % mon_basedir)
        common.rscp(user, mon, '%s/keyring.tmp' % mon_basedir, '%s/keyring' % mon_basedir )
        for node in osds:
            common.scp(user, node, '%s/keyring.tmp' % mon_basedir, '%s/keyring' % mon_basedir)
        # monmap
        cmd = 'monmaptool --create --clobber'
        for mon, addr in mons.items():
            cmd = cmd + ' --add %s %s' % (mon, addr)
        cmd = cmd + ' --print %s/monmap' % mon_basedir
        common.pdsh(user, [mon], cmd)
        common.rscp(user, mon, '%s/monmap.tmp' % mon_basedir, '%s/monmap' % mon_basedir)
        for node in mons:
            common.scp(user, node, '%s/monmap.tmp' % mon_basedir, '%s/monmap' % mon_basedir)

        # ceph-mons
        for mon, addr in mons.items():
            mon_filename = os.path.basename(self.cluster["ceph_conf"]["global"]["mon_data"]).replace("$id",mon)
            common.pdsh(user, [mon], 'rm -rf %s/%s' % (mon_basedir, mon_filename))
            common.pdsh(user, [mon], 'mkdir -p %s/%s' % (mon_basedir, mon_filename))
            common.pdsh(user, [mon], 'sh -c "ulimit -c unlimited && exec ceph-mon --mkfs -i %s --monmap=%s/monmap --keyring=%s/keyring"' % (mon, mon_basedir, mon_basedir))
            common.pdsh(user, [mon], 'cp %s/keyring %s/%s/keyring' % (mon_basedir, mon_basedir, mon_filename))
            
        # Start the mons
        for mon, addr in mons.items():
            common.pdsh(user, [mon], 'mkdir -p %s/pid' % mon_basedir)
            pidfile="%s/pid/%s.pid" % (mon_basedir, mon)
            cmd = 'sudo sh -c "ulimit -c unlimited && exec ceph-mon -i %s --keyring=%s/keyring --pid-file=%s"' % (mon, mon_basedir, pidfile)
            cmd = 'ceph-run %s' % cmd
            common.pdsh(user, [mon], '%s' % cmd, option="check_return")
            print common.bcolors.OKGREEN + "[LOG]Builded mon.%s daemon on %s" % (mon, mon) +common.bcolors.ENDC

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Deploy tool')
    parser.add_argument(
        'operation',
        help = 'only support redeploy now',
        )
    args = parser.parse_args()
    if args.operation == "redeploy":
        mydeploy = Deploy()
        mydeploy.redeploy()
