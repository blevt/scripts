#!/usr/bin/env python2

# this manages the currently running flow graph
# future: receive commands from network

# ./payload_2.py
# ./mockcommand.py 127.0.0.1

import math, sys, os, socket, time, struct, traceback, binascii, logging
#from threading import Thread
import threading
from datareporter import TCPDataReporter, UDPDataReporter
from subprocess import Popen, PIPE
from logger import *
from data_packers import *

def main():
    start = time.time()
    t = lambda: time.time()-start
    #print t

    while 1: # this loop repeats for every new connection
        try:
            # we completely tear down and recreate the socket every time in case of weirdness
            print("listening for new command connection...")
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", 2600))
            # we only handle a single connection at a time, so this isn't in its own thread
            s.listen(1)
            c, info = s.accept()
            print("command connection received from {}".format(info))
            f = FlowGraphManager(info[0])
            while 1:
                cmd = c.recv(1024).strip()
                if not cmd:
                    raise IOError("command protocol error: empty command")
                f.processinput(cmd)

        except f.Quit:
            print("payload processor restarted by command")

        except Exception as e:
            traceback.print_exc()

        finally:
            if 'f' in locals().keys():
                f.destroy()
            if 'c' in locals().keys():
                c.close()
            s.close()
            time.sleep(1) # limit cpu usage in case of repeated errors setting up connection

class FlowGraphManager():
    class Quit(IOError): pass # used to signal a shutdown requested by command

    def __init__(self, datatarget):
        self.topblock = None
        self.r = UDPDataReporter((datatarget, 1337))

    def processinput(self, cmd):
        cmd = cmd.strip().lower()
        self.r.checkfailure()
        if cmd in TopBlockThread.mode_handlers.keys():
            self.start(cmd)
        elif cmd == "stop":
            self.stop()
        elif cmd in ["quit", "exit"]:
            raise self.Quit(cmd)
        else:
            print("unrecognized command: '{}'".format(cmd))

    def start(self, mode="adsb"):
        self.stop()
        self.topblock = TopBlockThread(mode, self.r)
        self.topblock.start()

    def stop(self):
        if not self.topblock:
            return
        print 'FlowgraphManager'
        self.topblock.stop()
        self.topblock.join()
        self.topblock = None

    def destroy(self):
        self.stop()
        self.r.stop()


class TopBlockThread(threading.Thread):
    def __init__(self, mode, r):
        #super(TopBlockThread, self).__init__(self, name = 'TopBlockThread')
        threading.Thread.__init__(self, name = 'TopBlockThread')
        self._stop      = threading.Event()
        self.mode = mode
        self.r = r
        self.p = None
        self.adsb_part = ADSBPacker()
        self.ais_part = AISPacker()
        uptime_seconds = get_uptime()
        self.logger = setup_logger(mode, uptime_seconds)
        if self.mode not in self.mode_handlers.keys():
            raise ValueError("TopBlockThread mode {} not supported, try {}".format(
                    self.mode, self.mode_handlers))

    def _process_adsb(self, data):
        msg = data.strip().split()[0].decode("hex")
        #pwr = float(data.strip().split()[2])
        #pwr = 20*math.log10(pwr)
        #print pwr
        
        try:
            self.adsb_part.add(msg)
        except self.adsb_part.PacketFullError:
            # packetize the queued messages
            ret = str(self.adsb_part)
            # add the next message to the next packet
            self.adsb_part = ADSBPacker()
            self.adsb_part.add(msg)
            return ret

    def _process_ais(self, data):
        #raise NotImplementedError("no AIS data formatter yet")
        #print data
        msg = data.strip('\n')
        try:
            self.ais_part.add(msg)
        except self.ais_part.PacketFullError:
            # packetize the queued messages
            ret = str(self.ais_part)
            # add the next message to the next packet
            self.ais_part = AISPacker()
            self.ais_part.add(msg)
            return ret

    def _process_test(self, data):
        return data.strip()

    mode_binaries = {
            #I used the full path for adsb because we modified the stock modes_rx
            "adsb":     os.path.join(os.path.dirname(__file__), "modes_rx"),
            "ais":      "/home/root/waveforms/ais_rx.py",
            "testmode": "testmode/top_block.py",
    }
    mode_handlers = {
            "adsb":     _process_adsb,
            "ais":      _process_ais,
            "testmode": _process_adsb, # use testing data to exercise adsb packer
    }

    def new_run(self):
        print 'Running in mode:', self.mode
        
        if self.mode == 'adsb':
            self.p = Popen(self.mode_binaries[self.mode], stdout=PIPE)
            try:
                self._wait_for_load()
                while 1: 
                    line = self.p.stdout.readline()
                    self.logger.info(line)
                    if not line: # process finished
                        break
                    msg = self.mode_handlers[self.mode](self, line)
                    if msg:
                        self.r.send(self._pad_msg(msg))
            finally:
                #self.stop()
                print 'terminating'
        elif self.mode == 'ais':
            pass

    def run(self):
        print self.mode_binaries[self.mode]
        self.p = Popen(self.mode_binaries[self.mode], stdout=PIPE)
        try:
            self._wait_for_load()
            while 1: 
                line = self.p.stdout.readline()
                self.logger.info(line)
                if not line: # process finished
                    break
                msg = self.mode_handlers[self.mode](self, line)
                if msg:
                    self.r.send(self._pad_msg(msg))
        finally:
            #self.stop()
            print 'terminating'

    def _pad_msg(self, data):
        ret = ''
        if self.mode == 'adsb':
            ret = data.ljust(self.r.max_msg_len, "\0")
        elif self.mode == 'ais':
            #pad with special character
            ret = data.ljust(self.r.max_msg_len, "!")
        print("sending {:3d}: {}".format(len(ret), binascii.hexlify(ret)))
        return ret

    

    def _wait_for_load(self):
        if self.mode == "adsb":
            trace = ""
            while 1:
                line = self.p.stdout.readline()
                if not line:
                    raise IOError("failed to load {}, output so far:\n{}".format(self.mode, trace))
                if line.startswith("Using Volk machine: "):
                    #next line will be actual data
                    return
                trace += line
        elif self.mode == "ais":
            trace = ""
            cnt = 0
            while 1:
                line = self.p.stdout.readline()
                if not line:
                    raise IOError("failed to load {}, output so far:\n{}".format(self.mode, trace))
                if line.startswith("-- Performing timer loopback test... pass"):
                    if cnt == 0:
                        cnt += 1
                    elif cnt == 1:
                        #next line will be actual data
                        return
                trace += line

    def stop(self):
        if self.p:
            self.p.kill()
            #os.kill(self.p.pid, 0)
            self.p = None
        self._stop.set()
        print 'Stopping in mode:', self.mode

    def stopped(self):
        return self._stop.isSet()



if __name__ == "__main__":
    main()

