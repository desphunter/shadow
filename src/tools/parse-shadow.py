#!/usr/bin/python

import sys, os, argparse, re, json
from multiprocessing import Pool, cpu_count
from subprocess import Popen, PIPE
from signal import signal, SIGINT, SIG_IGN

DESCRIPTION="""
A utility to help parse results from the Shadow simulator.

This script enables processing of shadow log files and storing processed
data in json format for plotting. It was written so that the log file
need never be stored on disk decompressed, which is useful when log file
sizes reach tens of gigabytes.

Use the help menu to understand usage:
$ python parse-shadow.py -h

The standard way to run the script is to give the shadow.log file as
a positional argument:
$ python parse-shadow.py shadow.log
$ python parse-shadow.py shadow.log.xz

The log data can also be passed on STDIN with the special '-' filename:
$ cat shadow.log | python parse-shadow.py -
$ xzcat shadow.log.xz | python parse-shadow.py -

The default mode is to filter and parse the log file using a single
process; this will be done with multiple worker processes when passing
the '-m' option.\n
"""

SHADOWJSON="stats.shadow.json"
LABELS = ['bytes_total', 'bytes_control_header', 'bytes_control_header_retrans', 'bytes_data_header', 'bytes_data_payload', 'bytes_data_header_retrans', 'bytes_data_payload_retrans']
NUMLINES=10000

def main():
    parser = argparse.ArgumentParser(
        description=DESCRIPTION,
        formatter_class=argparse.RawTextHelpFormatter)#ArgumentDefaultsHelpFormatter)

    parser.add_argument(
        help="""The PATH to the shadow.log file, which may be '-'
for STDIN, or may end in '.xz' to enable inline
xz decompression""",
        metavar="PATH",
        action="store", dest="logpath")

    parser.add_argument('-m', '--multiproc',
        help="""Enable multiprocessing with N worker process, use '0'
to use the number of processor cores""",
        metavar="N",
        action="store", dest="nprocesses", type=type_nonnegative_integer,
        default=1)

    parser.add_argument('-p', '--prefix',
        help="""A STRING directory path prefix where the processed data
files generated by this script will be written""",
        metavar="STRING",
        action="store", dest="prefix",
        default=os.getcwd())

    parser.add_argument('-t', '--tee',
        help="""Echo decompressed log input back to stdout""",
        action="store_true", dest="tee",
        default=False)

    args = parser.parse_args()
    args.prefix = os.path.abspath(os.path.expanduser(args.prefix))
    if args.logpath != '-': args.logpath = os.path.abspath(os.path.expanduser(args.logpath))
    if args.nprocesses == 0: args.nprocesses = cpu_count()
    run(args)

def run(args):
    print >> sys.stderr, "processing input from {0}...".format(args.logpath)
    source, xzproc = source_prepare(args.logpath)

    d = {'ticks':{}, 'nodes':{}}
    m = {'mem':0, 'hours':0}
    p = Pool(args.nprocesses)
    try:
        lines = []
        for line in source:
            if args.tee: sys.stdout.write(line)
            lines.append(line)
            if len(lines) > args.nprocesses*NUMLINES:
                d, m = do_reduce(d, m, do_map(p, lines))
                lines = []
        if len(lines) > 0: d, m = do_reduce(d, m, do_map(p, lines))
        p.close()
    except KeyboardInterrupt:
        print >> sys.stderr, "interrupted, terminating process pool"
        p.terminate()
        p.join()
        sys.exit()

    source_cleanup(args.logpath, source, xzproc)

    print >> sys.stderr, "done processing input: simulation ran for {0} hours and consumed {1} GiB of RAM".format(m['hours'], m['mem'])
    print >> sys.stderr, "dumping stats in {0}".format(args.prefix)
    dump(d, args.prefix, SHADOWJSON)
    print >> sys.stderr, "all done!"

def do_map(pool, lines):
    mr = pool.map_async(process_shadow_lines, lines, NUMLINES)
    while not mr.ready(): mr.wait(1)
    return mr.get()

def do_reduce(data, m, result):
    for item in result:
        if item is None: continue

        max_mem, max_hours, d = item[0], item[1], item[2]
        if max_mem > m['mem']: m['mem'] = max_mem
        if max_hours > m['hours']: m['hours'] = max_hours

        for s in d['ticks']: data['ticks'][s] = d['ticks'][s]

        for n in d['nodes']:
            if n not in data['nodes']: data['nodes'][n] = {'recv':{}, 'send':{}}
            for l in LABELS:
                if l not in data['nodes'][n]['recv']: data['nodes'][n]['recv'][l] = {}
                if l not in data['nodes'][n]['send']: data['nodes'][n]['send'][l] = {}
                for s in d['nodes'][n]['recv'][l]:
                    if s not in data['nodes'][n]['recv']: data['nodes'][n]['recv'][l][s] = 0
                    if s not in data['nodes'][n]['send']: data['nodes'][n]['send'][l][s] = 0
                    data['nodes'][n]['recv'][l][s] += d['nodes'][n]['recv'][l][s]
                    data['nodes'][n]['send'][l][s] += d['nodes'][n]['send'][l][s]
    return data, m

def process_shadow_lines(line):
    signal(SIGINT, SIG_IGN) # ignore interrupts

    max_mem, max_seconds = 0, 0
    d = {'ticks':{}, 'nodes':{}}

    if re.search("slave_heartbeat", line) is not None:
        parts = line.strip().split()
        if len(parts) < 14: return None

        real_seconds = timestamp_to_seconds(parts[0])
        sim_seconds = 0
        # handle time format change from new scheduler/logger
        # this can go away once we merge 1.12.0, if we no longer want to support
        # log files created with older shadow versions
        maxrss_index = 13
        if parts[2] == 'n/a':
            if 'getrusage' not in parts[12]:
                sim_seconds = int(parts[12])/1000000000.0
                maxrss_index = 16
            else:
                maxrss_index = 13
        else:
            sim_seconds = timestamp_to_seconds(parts[2])
            maxrss_index = 13

        second = int(sim_seconds)
        maxrss = float(parts[maxrss_index].split('=')[1]) if 'maxrss' in parts[maxrss_index] else -1.0
        if second not in d['ticks']: d['ticks'][second] = {'time_seconds':real_seconds, 'maxrss_gib':maxrss}

        if maxrss > max_mem: max_mem = maxrss
        if real_seconds > max_seconds: max_seconds = real_seconds

    elif re.search("shadow-heartbeat", line) is not None:
        parts = line.strip().split()
        if len(parts) < 10 or '[node]' != parts[8]: return None

        real_seconds = timestamp_to_seconds(parts[0])
        if real_seconds > max_seconds: max_seconds = real_seconds
        sim_seconds = timestamp_to_seconds(parts[2])
        second = int(sim_seconds)

        name = parts[4].lstrip('[').rstrip(']') # eg: [webclient2-11.0.5.99]

        mods = parts[9].split(';')
        #nodestats = mods[0].split(',')
        #localin = mods[1].split(',')
        #localout = mods[2].split(',')
        remotein = mods[3].split(',')
        remoteout = mods[4].split(',')

        if name not in d['nodes']:
            d['nodes'][name] = {'recv':{}, 'send':{}}
            for label in LABELS:
                d['nodes'][name]['recv'][label] = {}
                d['nodes'][name]['send'][label] = {}
        for label in LABELS:
            if second not in d['nodes'][name]['recv'][label]: d['nodes'][name]['recv'][label][second] = 0
            if second not in d['nodes'][name]['send'][label]: d['nodes'][name]['send'][label][second] = 0

        '''
        a packet is a data packet if it contains a payload, and a control packet otherwise.
        each packet potentially has a header and a payload, and each packet is either
        a first transmission or a re-transmission.

        shadow prints the following in its heartbeat messages for the bytes counters:
        packets-total,bytes-total,
        packets-control,bytes-control-header,
        packets-control-retrans,bytes-control-header-retrans,
        packets-data,bytes-data-header,bytes-data-payload,
        packets-data-retrans,bytes-data-header-retrans,bytes-data-payload-retrans
        '''
        # packet counts are also available, but we are ignoring them
        d['nodes'][name]['recv']['bytes_total'][second] += int(remotein[1])
        d['nodes'][name]['recv']['bytes_control_header'][second] += int(remotein[3])
        d['nodes'][name]['recv']['bytes_control_header_retrans'][second] += int(remotein[5])
        d['nodes'][name]['recv']['bytes_data_header'][second] += int(remotein[7])
        d['nodes'][name]['recv']['bytes_data_payload'][second] += int(remotein[8])
        d['nodes'][name]['recv']['bytes_data_header_retrans'][second] += int(remotein[10])
        d['nodes'][name]['recv']['bytes_data_payload_retrans'][second] += int(remotein[11])

        d['nodes'][name]['send']['bytes_total'][second] += int(remoteout[1])
        d['nodes'][name]['send']['bytes_control_header'][second] += int(remoteout[3])
        d['nodes'][name]['send']['bytes_control_header_retrans'][second] += int(remoteout[5])
        d['nodes'][name]['send']['bytes_data_header'][second] += int(remoteout[7])
        d['nodes'][name]['send']['bytes_data_payload'][second] += int(remoteout[8])
        d['nodes'][name]['send']['bytes_data_header_retrans'][second] += int(remoteout[10])
        d['nodes'][name]['send']['bytes_data_payload_retrans'][second] += int(remoteout[11])

    return [max_mem, max_seconds/3600.0, d]

def type_nonnegative_integer(value):
    i = int(value)
    if i < 0: raise argparse.ArgumentTypeError("%s is an invalid non-negative int value" % value)
    return i

def source_prepare(filename):
    source, xzproc = None, None
    if filename == '-':
        source = sys.stdin
    elif filename.endswith(".xz"):
        xzproc = Popen(["xz", "--decompress", "--stdout", filename], stdout=PIPE)
        source = xzproc.stdout
    else:
        source = open(filename, 'r')
    return source, xzproc

def source_cleanup(filename, source, xzproc):
    if xzproc is not None: xzproc.wait()
    elif filename != '-': source.close()

def timestamp_to_seconds(stamp):
    parts = stamp.split(":")
    h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
    seconds = h*3600.0 + m*60.0 + s
    return seconds

def dump(data, prefix, filename, compress=True):
    if not os.path.exists(prefix): os.makedirs(prefix)
    if compress: # inline compression
        path = "{0}/{1}.xz".format(prefix, filename)
        xzp = Popen(["xz", "--threads=3", "-"], stdin=PIPE, stdout=PIPE)
        ddp = Popen(["dd", "status=none", "of={0}".format(path)], stdin=xzp.stdout)
        json.dump(data, xzp.stdin, sort_keys=True, separators=(',', ': '), indent=2)
        xzp.stdin.close()
        xzp.wait()
        ddp.wait()
    else: # no compression
        path = "{0}/{1}".format(prefix, filename)
        with open(path, 'w') as outf: json.dump(data, outf, sort_keys=True, separators=(',', ': '), indent=2)

if __name__ == '__main__': sys.exit(main())
