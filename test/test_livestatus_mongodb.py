#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (C) 2009-2010:
#    Gabes Jean, naparuba@gmail.com
#    Gerhard Lausser, Gerhard.Lausser@consol.de
#
# This file is part of Shinken.
#
# Shinken is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Shinken is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Shinken.  If not, see <http://www.gnu.org/licenses/>.


#
# This file is used to test host- and service-downtimes.
#


from __future__ import print_function

import os
import socket
import sys
import re
import subprocess
import time
import random
import tempfile


#sys.path.append('../shinken/modules')

from shinken_modules import ShinkenModulesTest
from shinken_test import time_hacker, unittest

from shinken.modulesctx import modulesctx
from shinken.objects.module import Module
from shinken.comment import Comment
from shinken.objects.service import Service


from mock_livestatus import mock_livestatus_handle_request



from livestatus.log_line import Logline


LiveStatusLogStoreMongoDB = modulesctx.get_module('logstore-mongodb').LiveStatusLogStoreMongoDB


sys.setcheckinterval(10000)


@mock_livestatus_handle_request
class TestConfig(ShinkenModulesTest):

    # how much seconds give to mongod be fully started
    # == listening on its input socket/port.
    mongod_start_timeout = 60

    @classmethod
    def _read_mongolog_and_raise(cls, log, proc, reason):
        try:
            with open(log) as fh:
                mongolog = fh.read()
        except Exception as err:
            mongolog = "Couldn't read log from mongo log file: %s" % err
        raise RuntimeError(
            "%s: rc=%s stdout/err=%s ; monglog=%s" % (
            reason, proc.returncode, proc.stdout.read(), mongolog))

    @classmethod
    def setUpClass(cls):
        # temp path for mongod files :
        # as you can see it's relative path, that'll be relative to where the test is launched,
        # which should be in the Shinken test directory.
        mongo_path = cls._mongo_tmp_path = tempfile.mkdtemp(dir="./tmp/", prefix="mongo")
        mongo_db = os.path.join(mongo_path, 'db')
        mongo_log = os.path.join(mongo_path, 'log.txt')
        os.system('/bin/rm -rf %r' % mongo_path)
        os.makedirs(mongo_db)
        print('Starting embedded mongo daemon..')
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        cls.mongo_db_uri = "mongodb://127.0.0.1:%s" % port
        mongo_args = ['/usr/bin/mongod', '--dbpath', mongo_db, '--port',
                      str(port), '--logpath', mongo_log, '--smallfiles']
        mp = cls._mongo_proc = subprocess.Popen(
            mongo_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False)
        print('Giving it some secs to correctly start..')
        time_hacker.set_real_time()
        # mongo takes some time to startup as it creates freshly new database files
        # so we need a relatively big timeout:
        timeout = time.time() + cls.mongod_start_timeout
        while time.time() < timeout:
            time.sleep(1)
            mp.poll()
            if mp.returncode is not None:
                cls._read_mongolog_and_raise(mongo_log, mp,
                                             "Launched mongod but it's directly died")

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            errno = sock.connect_ex(('127.0.0.1', port))
            if not errno:
                sock.close()
                break
        else:
            mp.kill()
            cls._read_mongolog_and_raise(
                mongo_log, mp,
                "could not connect to port %s : mongod failed to correctly start?" % port)

        time_hacker.set_my_time()

    @classmethod
    def tearDownClass(cls):
        mp = cls._mongo_proc
        mp.terminate()
        print('Waiting mongod server to exit ..')
        time_hacker.set_real_time()
        for _ in range(10):
            time.sleep(2)
            if mp.poll() is not None:
                break
        else:
            print("didn't exited after 10 secs ! killing it..")
            mp.kill()
        mp.wait()
        os.system('/bin/rm -rf %r' % cls._mongo_tmp_path)


    def tearDown(self):
        self.livestatus_broker.db.commit()
        self.livestatus_broker.db.close()
        if os.path.exists(self.livelogs):
            os.remove(self.livelogs)
        if os.path.exists(self.livelogs + "-journal"):
            os.remove(self.livelogs + "-journal")
        if os.path.exists("tmp/archives"):
            for db in os.listdir("tmp/archives"):
                print("cleanup", db)
                os.remove(os.path.join("tmp/archives", db))
        if os.path.exists('var/nagios.log'):
            os.remove('var/nagios.log')
        if os.path.exists('var/retention.dat'):
            os.remove('var/retention.dat')
        if os.path.exists('var/status.dat'):
            os.remove('var/status.dat')
        self.livestatus_broker = None



@mock_livestatus_handle_request
class TestConfigSmall(TestConfig):
    def setUp(self):
        #super(TestConfigSmall, self).setUp()
        self.setup_with_file('etc/shinken_1r_1h_1s.cfg')
        Comment.id = 1
        self.testid = str(os.getpid() + random.randint(1, 1000))

        dbmodconf = Module({'module_name': 'LogStore',
            'module_type': 'logstore_mongodb',
            'mongodb_uri': self.mongo_db_uri,
            'database': 'testtest' + self.testid,
        })

        self.init_livestatus(dbmodconf=dbmodconf)
        print("Cleaning old broks?")
        self.sched.conf.skip_initial_broks = False
        self.sched.brokers['Default-Broker'] = {'broks' : {}, 'has_full_broks' : False}
        self.sched.fill_initial_broks('Default-Broker')

        self.update_broker()
        self.nagios_path = None
        self.livestatus_path = None
        self.nagios_config = None
        # add use_aggressive_host_checking so we can mix exit codes 1 and 2
        # but still get DOWN state
        host = self.sched.hosts.find_by_name("test_host_0")
        host.__class__.use_aggressive_host_checking = 1


    def test_one_log(self):
        self.print_header()
        host = self.sched.hosts.find_by_name("test_host_0")
        now = time.time()
        time_hacker.time_warp(-3600)
        num_logs = 0
        host.state = 'DOWN'
        host.state_type = 'SOFT'
        host.attempt = 1
        host.output = "i am down"
        host.raise_alert_log_entry()
        time.sleep(3600)
        host.state = 'UP'
        host.state_type = 'HARD'
        host.attempt = 1
        host.output = "i am up"
        host.raise_alert_log_entry()
        time.sleep(3600)
        self.update_broker()
        print("-------------------------------------------")
        print("Service.lsm_host_name", Service.lsm_host_name)
        print("Logline.lsm_current_host_name", Logline.lsm_current_host_name)
        print("-------------------------------------------")

        print("request logs from", int(now - 3600), int(now + 3600))
        print("request logs from",
              time.asctime(time.localtime(int(now - 3600))),
              time.asctime(time.localtime(int(now + 3600))))
        request = """GET log
Filter: time >= """ + str(int(now - 3600)) + """
Filter: time <= """ + str(int(now + 3600)) + """
Columns: time type options state host_name"""
        response, keepalive = self.livestatus_broker.livestatus.handle_request(request)
        print(response)
        name = 'testtest' + self.testid
        numlogs = self.livestatus_broker.db.conn[name].logs.find().count()
        print(numlogs)
        self.assert_(numlogs == 2)
        curs = self.livestatus_broker.db.conn[name].logs.find()
        self.assert_(curs[0]['state_type'] == 'SOFT')
        self.assert_(curs[1]['state_type'] == 'HARD')


@mock_livestatus_handle_request
class TestConfigBig(TestConfig):
    def setUp(self):
        super(TestConfigBig, self).setUp()
        start_setUp = time.time()
        self.setup_with_file('etc/shinken_5r_100h_2000s.cfg')
        Comment.id = 1
        self.testid = str(os.getpid() + random.randint(1, 1000))

        dbmodconf = Module({'module_name': 'LogStore',
            'module_type': 'logstore_mongodb',
            'mongodb_uri': self.mongo_db_uri,
            'database': 'testtest' + self.testid,
        })

        self.init_livestatus(dbmodconf=dbmodconf)
        print("Cleaning old broks?")
        self.sched.conf.skip_initial_broks = False
        self.sched.brokers['Default-Broker'] = {'broks' : {}, 'has_full_broks' : False}
        self.sched.fill_initial_broks('Default-Broker')

        self.update_broker()
        print("************* Overall Setup:", time.time() - start_setUp)
        # add use_aggressive_host_checking so we can mix exit codes 1 and 2
        # but still get DOWN state
        host = self.sched.hosts.find_by_name("test_host_000")
        host.__class__.use_aggressive_host_checking = 1


    def count_log_broks(self):
        return len([brok for brok in self.sched.broks.values() if brok.type == 'log'])


    def test_a_long_history(self):
        # copied from test_livestatus_cache
        test_host_005 = self.sched.hosts.find_by_name("test_host_005")
        test_host_099 = self.sched.hosts.find_by_name("test_host_099")
        find = self.sched.services.find_srv_by_name_and_hostname
        test_ok_00 = find("test_host_005", "test_ok_00")
        test_ok_01 = find("test_host_005", "test_ok_01")
        test_ok_04 = find("test_host_005", "test_ok_04")
        test_ok_16 = find("test_host_005", "test_ok_16")
        test_ok_99 = find("test_host_099", "test_ok_01")

        days = 4
        etime = time.time()
        print("now it is", time.ctime(etime))
        print("now it is", time.gmtime(etime))
        etime_midnight = (etime - (etime % 86400)) + time.altzone
        print("midnight was", time.ctime(etime_midnight))
        print("midnight was", time.gmtime(etime_midnight))
        query_start = etime_midnight - (days - 1) * 86400
        query_end = etime_midnight
        print("query_start", time.ctime(query_start))
        print("query_end ", time.ctime(query_end))

        # |----------|----------|----------|----------|----------|---x
        #                                                            etime
        #                                                        etime_midnight
        #             ---x------
        #                etime -  4 days
        #                       |---
        #                       query_start
        #
        #                ............................................
        #                events in the log database ranging till now
        #
        #                       |________________________________|
        #                       events which will be read from db
        #
        loops = int(86400 / 192)
        time_hacker.time_warp(-1 * days * 86400)
        print("warp back to", time.ctime(time.time()))
        # run silently
        old_stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")
        should_be = 0
        for day in xrange(days):
            sys.stderr.write("day %d now it is %s i run %d loops\n" % (
                day, time.ctime(time.time()), loops))
            self.scheduler_loop(2, [
                [test_ok_00, 0, "OK"],
                [test_ok_01, 0, "OK"],
                [test_ok_04, 0, "OK"],
                [test_ok_16, 0, "OK"],
                [test_ok_99, 0, "OK"],
            ])
            self.update_broker()
            #for i in xrange(3600 * 24 * 7):
            for i in xrange(loops):
                if i % 10000 == 0:
                    sys.stderr.write(str(i))
                if i % 399 == 0:
                    self.scheduler_loop(3, [
                        [test_ok_00, 1, "WARN"],
                        [test_ok_01, 2, "CRIT"],
                        [test_ok_04, 3, "UNKN"],
                        [test_ok_16, 1, "WARN"],
                        [test_ok_99, 2, "CRIT"],
                    ])
                    if query_start <= int(time.time()) <= query_end:
                        should_be += 3
                        sys.stderr.write("now it should be %s\n" % should_be)
                time.sleep(62)
                if i % 399 == 0:
                    self.scheduler_loop(1, [
                        [test_ok_00, 0, "OK"],
                        [test_ok_01, 0, "OK"],
                        [test_ok_04, 0, "OK"],
                        [test_ok_16, 0, "OK"],
                        [test_ok_99, 0, "OK"],
                    ])
                    if query_start <= int(time.time()) <= query_end:
                        should_be += 1
                        sys.stderr.write("now it should be %s\n" % should_be)
                time.sleep(2)
                if i % 9 == 0:
                    self.scheduler_loop(3, [
                        [test_ok_00, 1, "WARN"],
                        [test_ok_01, 2, "CRIT"],
                    ])

                time.sleep(62)
                if i % 9 == 0:
                    self.scheduler_loop(1, [
                        [test_ok_00, 0, "OK"],
                        [test_ok_01, 0, "OK"],
                    ])
                time.sleep(2)
                if i % 9 == 0:
                    self.scheduler_loop(3, [
                        [test_host_005, 2, "DOWN"],
                    ])
                if i % 2 == 0:
                    self.scheduler_loop(3, [
                        [test_host_099, 2, "DOWN"],
                    ])
                time.sleep(62)
                if i % 9 == 0:
                    self.scheduler_loop(3, [
                        [test_host_005, 0, "UP"],
                    ])
                if i % 2 == 0:
                    self.scheduler_loop(3, [
                        [test_host_099, 0, "UP"],
                    ])
                time.sleep(2)
                self.update_broker()
                if i % 1000 == 0:
                    self.livestatus_broker.db.commit()
            endtime = time.time()
            self.livestatus_broker.db.commit()
            sys.stderr.write("day %d end it is %s\n" % (day, time.ctime(time.time())))
        sys.stdout.close()
        sys.stdout = old_stdout
        self.livestatus_broker.db.commit_and_rotate_log_db()
        name = 'testtest' + self.testid
        numlogs = self.livestatus_broker.db.conn[name].logs.find().count()
        print("numlogs is", numlogs)

        # now we have a lot of events
        # find type = HOST ALERT for test_host_005
        columns = (
            "class time type state host_name service_description plugin_output message options "
            "contact_name command_name state_type current_host_groups current_service_groups"
        )
        request = """GET log
Columns: """ + columns + """
Filter: time >= """ + str(int(query_start)) + """
Filter: time <= """ + str(int(query_end)) + """
Filter: type = SERVICE ALERT
And: 1
Filter: type = HOST ALERT
And: 1
Filter: type = SERVICE FLAPPING ALERT
Filter: type = HOST FLAPPING ALERT
Filter: type = SERVICE DOWNTIME ALERT
Filter: type = HOST DOWNTIME ALERT
Filter: type ~ starting...
Filter: type ~ shutting down...
Or: 8
Filter: host_name = test_host_099
Filter: service_description = test_ok_01
And: 5
OutputFormat: json"""
        # switch back to realtime. we want to know how long it takes
        time_hacker.set_real_time()

        print(request)
        print("query 1 --------------------------------------------------")
        tic = time.time()
        response, keepalive = self.livestatus_broker.livestatus.handle_request(request)
        tac = time.time()
        pyresponse = eval(response)
        print(response)
        print("number of records with test_ok_01", len(pyresponse))
        self.assert_(len(pyresponse) == should_be)

        # and now test Negate:
        request = """GET log
Filter: time >= """ + str(int(query_start)) + """
Filter: time <= """ + str(int(query_end)) + """
Filter: type = SERVICE ALERT
And: 1
Filter: type = HOST ALERT
And: 1
Filter: type = SERVICE FLAPPING ALERT
Filter: type = HOST FLAPPING ALERT
Filter: type = SERVICE DOWNTIME ALERT
Filter: type = HOST DOWNTIME ALERT
Filter: type ~ starting...
Filter: type ~ shutting down...
Or: 8
Filter: host_name = test_host_099
Filter: service_description = test_ok_01
And: 2
Negate:
And: 2
OutputFormat: json"""
        response, keepalive = self.livestatus_broker.livestatus.handle_request(request)
        notpyresponse = eval(response)
        print("number of records without test_ok_01", len(notpyresponse))

        request = """GET log
Filter: time >= """ + str(int(query_start)) + """
Filter: time <= """ + str(int(query_end)) + """
Filter: type = SERVICE ALERT
And: 1
Filter: type = HOST ALERT
And: 1
Filter: type = SERVICE FLAPPING ALERT
Filter: type = HOST FLAPPING ALERT
Filter: type = SERVICE DOWNTIME ALERT
Filter: type = HOST DOWNTIME ALERT
Filter: type ~ starting...
Filter: type ~ shutting down...
Or: 8
OutputFormat: json"""
        response, keepalive = self.livestatus_broker.livestatus.handle_request(request)
        allpyresponse = eval(response)
        print("all records", len(allpyresponse))
        self.assert_(len(allpyresponse) == len(notpyresponse) + len(pyresponse))


        # Now a pure class check query
        request = """GET log
Filter: time >= """ + str(int(query_start)) + """
Filter: time <= """ + str(int(query_end)) + """
Filter: class = 1
OutputFormat: json"""
        response, keepalive = self.livestatus_broker.livestatus.handle_request(request)
        allpyresponse = eval(response)
        print("all records", len(allpyresponse))
        self.assert_(len(allpyresponse) == len(notpyresponse) + len(pyresponse))

        # now delete too old entries from the database (> 14days)
        # that's the job of commit_and_rotate_log_db()


        numlogs = self.livestatus_broker.db.conn[name].logs.find().count()
        times = [x['time'] for x in self.livestatus_broker.db.conn[name].logs.find()]
        self.assert_(times != [])
        print("whole database", numlogs, min(times), max(times))
        numlogs = self.livestatus_broker.db.conn[name].logs.find({
            '$and': [
                {'time': {'$gt': min(times)}},
                {'time': {'$lte': max(times)}}
            ]}).count()
        now = max(times)
        daycount = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        for day in xrange(25):
            one_day_earlier = now - 3600*24
            numlogs = self.livestatus_broker.db.conn[name].logs.find({
                '$and': [
                    {'time': {'$gt': one_day_earlier}},
                    {'time': {'$lte': now}}
                ]}).count()
            daycount[day] = numlogs
            print("day -%02d %d..%d - %d" % (day, one_day_earlier, now, numlogs))
            now = one_day_earlier
        self.livestatus_broker.db.commit_and_rotate_log_db()
        now = max(times)
        for day in xrange(25):
            one_day_earlier = now - 3600*24
            numlogs = self.livestatus_broker.db.conn[name].logs.find({
                '$and': [
                    {'time': {'$gt': one_day_earlier}},
                    {'time': {'$lte': now}}
                ]}).count()
            print("day -%02d %d..%d - %d" % (day, one_day_earlier, now, numlogs))
            now = one_day_earlier
        numlogs = self.livestatus_broker.db.conn[name].logs.find().count()
        # simply an estimation. the cleanup-routine in the mongodb logstore
        # cuts off the old data at midnight, but here in the test we have
        # only accuracy of a day.
        self.assert_(numlogs >= sum(daycount[:7]))
        self.assert_(numlogs <= sum(daycount[:8]))

        time_hacker.set_my_time()

    def test_max_logs_age(self):
        dbmodconf = Module({'module_name': 'LogStore',
            'module_type': 'logstore_mongodb',
            'database': 'bigbigbig',
            'mongodb_uri': self.mongo_db_uri,
            'max_logs_age': '7y',
        })

        print(dbmodconf.max_logs_age)
        livestatus_broker = LiveStatusLogStoreMongoDB(dbmodconf)
        self.assertEqual(7*365, livestatus_broker.max_logs_age)



if __name__ == '__main__':
    #import cProfile
    command = """unittest.main()"""
    unittest.main()
    #cProfile.runctx( command, globals(), locals(), filename="/tmp/livestatus.profile" )
