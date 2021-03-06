# Copyright 2009-2012 Yelp
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test the idle job flow terminator"""

from __future__ import with_statement

from StringIO import StringIO
from datetime import datetime
from datetime import timedelta
import sys

from mrjob.pool import est_time_to_hour
from mrjob.pool import pool_hash_and_name
from mrjob.tools.emr.terminate_idle_job_flows import (
    inspect_and_maybe_terminate_job_flows,)
from mrjob.tools.emr.terminate_idle_job_flows import is_job_flow_bootstrapping
from mrjob.tools.emr.terminate_idle_job_flows import is_job_flow_done
from mrjob.tools.emr.terminate_idle_job_flows import is_job_flow_running
from mrjob.tools.emr.terminate_idle_job_flows import is_job_flow_streaming
from mrjob.tools.emr.terminate_idle_job_flows import job_flow_has_pending_steps
from mrjob.tools.emr.terminate_idle_job_flows import time_last_active

from tests.mockboto import MockEmrObject
from tests.mockboto import to_iso8601
from tests.mockboto import MockEmrConnection
from tests.test_emr import MockEMRAndS3TestCase


class JobFlowInspectionTestCase(MockEMRAndS3TestCase):

    def setUp(self):
        super(JobFlowInspectionTestCase, self).setUp()
        self.create_fake_job_flows()

    def create_fake_job_flows(self):
        self.now = datetime.utcnow().replace(microsecond=0)

        # Build a step object easily
        # also make it respond to .args()
        def step(jar='/home/hadoop/contrib/streaming/hadoop-streaming.jar',
                 args=['-mapper', 'my_job.py --mapper',
                       '-reducer', 'my_job.py --reducer'],
                 state='COMPLETE',
                 create_hours_ago=None,
                 start_hours_ago=None,
                 end_hours_ago=None,
                 name='Streaming Step',
                 action_on_failure='TERMINATE_JOB_FLOW',
                 **kwargs):
            if create_hours_ago:
                kwargs['creationdatetime'] = to_iso8601(
                    self.now - timedelta(hours=create_hours_ago))
            if start_hours_ago:
                kwargs['startdatetime'] = to_iso8601(
                    self.now - timedelta(hours=start_hours_ago))
            if end_hours_ago:
                kwargs['enddatetime'] = to_iso8601(
                    self.now - timedelta(hours=end_hours_ago))
            kwargs['args'] = [MockEmrObject(value=a) for a in args]
            return MockEmrObject(
                jar=jar, state=state, name=name,
                action_on_failure=action_on_failure, **kwargs)

        # empty job
        self.mock_emr_job_flows['j-EMPTY'] = MockEmrObject(
            creationdatetime=to_iso8601(self.now - timedelta(hours=10)),
            state='STARTING',
        )

        # job that's bootstrapping
        self.mock_emr_job_flows['j-BOOTSTRAPPING'] = MockEmrObject(
            creationdatetime=to_iso8601(self.now - timedelta(hours=10)),
            startdatetime=to_iso8601(
                self.now - timedelta(hours=9, minutes=55)),
            state='BOOTSTRAPPING',
            steps=[step(create_hours_ago=10, state='PENDING')],
        )

        # currently running job
        self.mock_emr_job_flows['j-CURRENTLY_RUNNING'] = MockEmrObject(
            creationdatetime=to_iso8601(self.now - timedelta(hours=6)),
            readydatetime=to_iso8601(self.now - timedelta(hours=4,
                                                          minutes=10)),
            startdatetime=to_iso8601(self.now - timedelta(hours=4,
                                                          minutes=15)),
            state='RUNNING',
            steps=[step(start_hours_ago=4, state='RUNNING')],
        )

        # finished job flow
        self.mock_emr_job_flows['j-DONE'] = MockEmrObject(
            creationdatetime=to_iso8601(self.now - timedelta(hours=10)),
            enddatetime=to_iso8601(self.now - timedelta(hours=5)),
            readydatetime=to_iso8601(self.now - timedelta(hours=8)),
            startdatetime=to_iso8601(self.now - timedelta(hours=9)),
            state='COMPLETE',
            steps=[step(start_hours_ago=8, end_hours_ago=6)],
        )

        # idle job flow
        self.mock_emr_job_flows['j-DONE_AND_IDLE'] = MockEmrObject(
            creationdatetime=to_iso8601(self.now - timedelta(hours=6)),
            readydatetime=to_iso8601(self.now - timedelta(hours=5, minutes=5)),
            startdatetime=to_iso8601(self.now - timedelta(hours=5)),
            state='WAITING',
            steps=[step(start_hours_ago=4, end_hours_ago=2)],
        )

        # hive job flow (looks completed but isn't)
        self.mock_emr_job_flows['j-HIVE'] = MockEmrObject(
            creationdatetime=to_iso8601(self.now - timedelta(hours=6)),
            readydatetime=to_iso8601(self.now - timedelta(hours=5, minutes=5)),
            startdatetime=to_iso8601(self.now - timedelta(hours=5)),
            state='WAITING',
            steps=[step(
                start_hours_ago=4,
                end_hours_ago=4,
                jar=('s3://us-east-1.elasticmapreduce/libs/script-runner/'
                     'script-runner.jar'),
                args=[],
            )],
        )

        # custom hadoop streaming jar
        self.mock_emr_job_flows['j-CUSTOM_DONE_AND_IDLE'] = MockEmrObject(
            creationdatetime=to_iso8601(self.now - timedelta(hours=6)),
            readydatetime=to_iso8601(self.now - timedelta(hours=5, minutes=5)),
            startdatetime=to_iso8601(self.now - timedelta(hours=5)),
            state='WAITING',
            steps=[step(
                start_hours_ago=4,
                end_hours_ago=4,
                jar=('s3://my_bucket/tmp/somejob/files/'
                     'oddjob-0.0.3-SNAPSHOT-standalone.jar'),
                args=[],
            )],
        )

        mock_conn = MockEmrConnection()

        # hadoop debugging without any other steps
        jobflow_id = mock_conn.run_jobflow(name='j-DEBUG_ONLY',
                                           log_uri='',
                                           enable_debugging=True,
                                           now=self.now -
                                               timedelta(hours=3, minutes=5))
        jf = mock_conn.describe_jobflow(jobflow_id)
        self.mock_emr_job_flows['j-DEBUG_ONLY'] = jf
        jf.state = 'WAITING'
        jf.startdatetime = to_iso8601(
            self.now - timedelta(hours=3))
        jf.readydatetime = to_iso8601(
            self.now - timedelta(hours=2, minutes=55))
        jf.steps[0].enddatetime = to_iso8601(self.now - timedelta(hours=2))

        # hadoop debugging + actual job
        # same jar as hive but with different args
        jobflow_id = mock_conn.run_jobflow(name='j-HADOOP_DEBUGGING',
                                           log_uri='',
                                           enable_debugging=True,
                                           steps=[step()],
                                           now=self.now -
                                               timedelta(hours=6))
        jf = mock_conn.describe_jobflow(jobflow_id)
        self.mock_emr_job_flows['j-HADOOP_DEBUGGING'] = jf
        jf.state = 'WAITING'
        jf.startdatetime = to_iso8601(self.now - timedelta(hours=5))
        jf.readydatetime = to_iso8601(
            self.now - timedelta(hours=4, minutes=55))
        # Need to reset times manually because mockboto resets them
        jf.steps[0].enddatetime = to_iso8601(self.now - timedelta(hours=5))
        jf.steps[1].startdatetime = to_iso8601(self.now - timedelta(hours=4))
        jf.steps[1].enddatetime = to_iso8601(self.now - timedelta(hours=2))

        # should skip cancelled steps
        self.mock_emr_job_flows['j-IDLE_AND_FAILED'] = MockEmrObject(
            state='WAITING',
            creationdatetime=to_iso8601(self.now - timedelta(hours=6)),
            readydatetime=to_iso8601(self.now - timedelta(hours=5, minutes=5)),
            startdatetime=to_iso8601(self.now - timedelta(hours=5)),
            steps=[
                step(start_hours_ago=4, end_hours_ago=3, state='FAILED'),
                step(
                    state='CANCELLED',
                )
            ],
        )

        # pooled job flow reaching end of full hour
        self.mock_emr_job_flows['j-POOLED'] = MockEmrObject(
            bootstrapactions=[
                MockEmrObject(args=[]),
                MockEmrObject(args=[
                    MockEmrObject(
                        value='pool-0123456789abcdef0123456789abcdef'),
                    MockEmrObject(value='reflecting'),
                ]),
            ],
            creationdatetime=to_iso8601(self.now - timedelta(hours=1)),
            readydatetime=to_iso8601(self.now - timedelta(minutes=50)),
            startdatetime=to_iso8601(self.now - timedelta(minutes=55)),
            state='WAITING',
            steps=[],
        )

        # job flow that has had pending jobs but hasn't run them
        self.mock_emr_job_flows['j-PENDING_BUT_IDLE'] = MockEmrObject(
            creationdatetime=to_iso8601(self.now - timedelta(hours=3)),
            readydatetime=to_iso8601(
                self.now - timedelta(hours=2, minutes=50)),
            startdatetime=to_iso8601(
                self.now - timedelta(hours=2, minutes=55)),
            state='RUNNING',
            steps=[step(create_hours_ago=3, state='PENDING')],
        )

        # add job flow IDs and fake names to the mock job flows
        for jfid, jf in self.mock_emr_job_flows.iteritems():
            jf.jobflowid = jfid
            jf.name = jfid[2:].replace('_', ' ').title() + ' Job Flow'

    def terminated_jfs(self):
        return sorted(jf.jobflowid
                      for jf in self.mock_emr_job_flows.itervalues()
                      if jf.state in ('SHUTTING_DOWN', 'TERMINATED'))

    def inspect_and_maybe_terminate_quietly(self, **kwargs):
        if 'conf_path' not in kwargs:
            kwargs['conf_path'] = False

        if 'now' not in kwargs:
            kwargs['now'] = self.now

        # don't print anything out
        real_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            return inspect_and_maybe_terminate_job_flows(**kwargs)
        finally:
            sys.stdout = real_stdout

    def time_job_flow_idle(self, jf):
        if (is_job_flow_bootstrapping(jf) or
            is_job_flow_running(jf) or
            is_job_flow_done(jf)):
            return timedelta(0)
        else:
            return self.now - time_last_active(jf)

    def assertJobFlowIs(
        self, jf,
        bootstrapping=False,
        done=False,
        from_end_of_hour=timedelta(hours=1),
        has_pending_steps=False,
        idle_for=timedelta(0),
        pool_hash=None,
        pool_name=None,
        running=False,
        streaming=True,
    ):
        self.assertEqual(bootstrapping, is_job_flow_bootstrapping(jf))
        self.assertEqual(done, is_job_flow_done(jf))
        self.assertEqual(from_end_of_hour, est_time_to_hour(jf, self.now))
        self.assertEqual(has_pending_steps, job_flow_has_pending_steps(jf))
        self.assertEqual(idle_for, self.time_job_flow_idle(jf))
        self.assertEqual((pool_hash, pool_name), pool_hash_and_name(jf))
        self.assertEqual(running, is_job_flow_running(jf))
        self.assertEqual(streaming, is_job_flow_streaming(jf))

    def test_empty(self):
        self.assertJobFlowIs(
            self.mock_emr_job_flows['j-EMPTY'],
            idle_for=timedelta(hours=10),
        )

    def test_currently_running(self):
        self.assertJobFlowIs(
            self.mock_emr_job_flows['j-CURRENTLY_RUNNING'],
            from_end_of_hour=timedelta(minutes=45),
            running=True,
        )

    def test_done(self):
        self.assertJobFlowIs(
            self.mock_emr_job_flows['j-DONE'],
            done=True,
        )

    def test_debug_only(self):
        self.assertJobFlowIs(
            self.mock_emr_job_flows['j-DEBUG_ONLY'],
            idle_for=timedelta(hours=2),
        )

    def test_done_and_idle(self):
        self.assertJobFlowIs(
            self.mock_emr_job_flows['j-DONE_AND_IDLE'],
            idle_for=timedelta(hours=2),
        )

    def test_hive_job_flow(self):
        self.assertJobFlowIs(
            self.mock_emr_job_flows['j-HIVE'],
            idle_for=timedelta(hours=4),
            streaming=False,
        )

    def test_hadoop_debugging_job_flow(self):
        self.assertJobFlowIs(
            self.mock_emr_job_flows['j-HADOOP_DEBUGGING'],
            idle_for=timedelta(hours=2),
        )

    def test_idle_and_failed(self):
        self.assertJobFlowIs(
            self.mock_emr_job_flows['j-IDLE_AND_FAILED'],
            idle_for=timedelta(hours=3),
        )

    def test_pooled(self):
        self.assertJobFlowIs(
            self.mock_emr_job_flows['j-POOLED'],
            from_end_of_hour=timedelta(minutes=5),
            idle_for=timedelta(minutes=50),
            pool_hash='0123456789abcdef0123456789abcdef',
            pool_name='reflecting',
        )

    def test_pending_but_idle(self):
        self.assertJobFlowIs(
            self.mock_emr_job_flows['j-PENDING_BUT_IDLE'],
            from_end_of_hour=timedelta(minutes=5),
            has_pending_steps=True,
            idle_for=timedelta(hours=2, minutes=50),
        )

    def test_dry_run_does_nothing(self):
        self.inspect_and_maybe_terminate_quietly(
            max_hours_idle=0.01, dry_run=True)

        self.assertEqual(self.terminated_jfs(), [])

    def test_increasing_idle_time(self):
        self.assertEqual(self.terminated_jfs(), [])

        # no job flows are 20 hours old
        self.inspect_and_maybe_terminate_quietly(
            conf_path=False, max_hours_idle=20,
            now=self.now)

        # terminate 5-hour-old jobs
        self.inspect_and_maybe_terminate_quietly(
            conf_path=False, max_hours_idle=5,
            now=self.now)

        # j-HIVE is old enough to terminate, but it doesn't have streaming
        # steps, so we leave it alone
        self.assertEqual(self.terminated_jfs(), ['j-EMPTY'])

        # terminate 2-hour-old jobs
        self.inspect_and_maybe_terminate_quietly(
            conf_path=False, max_hours_idle=2,
            now=self.now)

        # picky edge case: two jobs are EXACTLY 2 hours old, so they're
        # not over the maximum

        self.assertEqual(self.terminated_jfs(),
                         ['j-EMPTY', 'j-IDLE_AND_FAILED',
                          'j-PENDING_BUT_IDLE'])

        self.inspect_and_maybe_terminate_quietly(max_hours_idle=1)

        self.assertEqual(self.terminated_jfs(),
                         ['j-DEBUG_ONLY', 'j-DONE_AND_IDLE', 'j-EMPTY',
                          'j-HADOOP_DEBUGGING', 'j-IDLE_AND_FAILED',
                          'j-PENDING_BUT_IDLE'])

    def test_one_hour_is_the_default(self):
        self.assertEqual(self.terminated_jfs(), [])

        self.inspect_and_maybe_terminate_quietly()

        self.assertEqual(self.terminated_jfs(),
                         ['j-DEBUG_ONLY', 'j-DONE_AND_IDLE', 'j-EMPTY',
                          'j-HADOOP_DEBUGGING', 'j-IDLE_AND_FAILED',
                          'j-PENDING_BUT_IDLE'])

    def test_zero_idle_time(self):
        self.assertEqual(self.terminated_jfs(), [])

        self.inspect_and_maybe_terminate_quietly(max_hours_idle=0)

        self.assertEqual(self.terminated_jfs(),
                         ['j-DEBUG_ONLY', 'j-DONE_AND_IDLE', 'j-EMPTY',
                          'j-HADOOP_DEBUGGING', 'j-IDLE_AND_FAILED',
                          'j-PENDING_BUT_IDLE', 'j-POOLED'])

    def test_mins_to_end_of_hour(self):

        self.inspect_and_maybe_terminate_quietly(mins_to_end_of_hour=2)

        self.assertEqual(self.terminated_jfs(), [])

        # edge case: it's exactly 5 minutes to end of hour
        self.inspect_and_maybe_terminate_quietly(mins_to_end_of_hour=5)

        self.assertEqual(self.terminated_jfs(), [])

        self.inspect_and_maybe_terminate_quietly(mins_to_end_of_hour=6)

        # j-PENDING_BUT_IDLE is also 5 mins from end of hour, but
        # is skipped because it has pending jobs.
        self.assertEqual(self.terminated_jfs(), ['j-POOLED'])

    def test_mins_to_end_of_hour_excludes_pending(self):
        # the filters are ANDed togther, and mins_to_end_of_hour excludes
        # jobs with pending steps.
        self.inspect_and_maybe_terminate_quietly(mins_to_end_of_hour=61,
                                                 max_hours_idle=0.01)

        self.assertEqual(self.terminated_jfs(),
                         ['j-DEBUG_ONLY', 'j-DONE_AND_IDLE', 'j-EMPTY',
                          'j-HADOOP_DEBUGGING', 'j-IDLE_AND_FAILED',
                         'j-POOLED'])

    def test_terminate_pooled_only(self):
        self.assertEqual(self.terminated_jfs(), [])

        self.inspect_and_maybe_terminate_quietly(pooled_only=True)

        # pooled job was not idle for an hour (the default)
        self.assertEqual(self.terminated_jfs(), [])

        self.inspect_and_maybe_terminate_quietly(
            pooled_only=True, max_hours_idle=0.01)

        self.assertEqual(self.terminated_jfs(), ['j-POOLED'])

    def test_terminate_unpooled_only(self):
        self.assertEqual(self.terminated_jfs(), [])

        self.inspect_and_maybe_terminate_quietly(unpooled_only=True)

        self.assertEqual(self.terminated_jfs(),
                         ['j-DEBUG_ONLY', 'j-DONE_AND_IDLE', 'j-EMPTY',
                          'j-HADOOP_DEBUGGING', 'j-IDLE_AND_FAILED',
                          'j-PENDING_BUT_IDLE'])

        self.inspect_and_maybe_terminate_quietly(
            unpooled_only=True, max_hours_idle=0.01)

        self.assertEqual(self.terminated_jfs(),
                         ['j-DEBUG_ONLY', 'j-DONE_AND_IDLE', 'j-EMPTY',
                          'j-HADOOP_DEBUGGING', 'j-IDLE_AND_FAILED',
                          'j-PENDING_BUT_IDLE'])

    def test_terminate_by_pool_name(self):
        self.assertEqual(self.terminated_jfs(), [])

        # wrong pool name
        self.inspect_and_maybe_terminate_quietly(
            pool_name='default', max_hours_idle=0.01)

        self.assertEqual(self.terminated_jfs(), [])

        # right pool name
        self.inspect_and_maybe_terminate_quietly(
            pool_name='reflecting', max_hours_idle=0.01)

        self.assertEqual(self.terminated_jfs(), ['j-POOLED'])
