"""Acceptance tests for the restoration and coverage gaps observed on the clone."""
import json
import os
import tempfile
import subprocess
import sys
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from playwright.sync_api import sync_playwright
from demo_app.regression_fixture import server
from discovery.crawler import Crawler, CrawlConfig
from discovery.coverage import merge_graphs
from discovery.graph import AppGraph, Action
from discovery.page_inspector import inspect_with_page
from knowledge.contracts import BehaviorContract
from knowledge.engine import Relation
from knowledge.flows import FlowTestInstance, PathStep, BindingSpec, evaluate_instance, path_steps
from baseline.saver import flow_test_record
from runner.flow_runner import FlowRunner, to_number


class CheckpointUnits(unittest.TestCase):
    def test_origin_boundary_is_exact(self):
        crawler=Crawler(None,'https://example.test')
        self.assertTrue(crawler._is_same_origin('/cart'))
        self.assertFalse(crawler._is_same_origin('https://example.test.evil.test/cart'))
        self.assertFalse(crawler._is_same_origin('//other.test/cart'))

    def test_numeric_labels_are_not_accounting_negatives(self):
        for text, expected in [('Subtotal (1 items) :',1),('Subtotal (-1 items) :',-1),
                               ('($600.00)',-600),('$-600',-600),('(-600)',-600)]:
            self.assertEqual(to_number(text),expected,text)

    def test_stock_and_quantity_price_rules(self):
        self.assertTrue(Relation.parse('quantity <= stock_limit').evaluate({'quantity':2,'stock_limit':16}))
        self.assertFalse(Relation.parse('quantity <= stock_limit').evaluate({'quantity':17,'stock_limit':16}))
        self.assertTrue(Relation.parse('product(factors) == total').evaluate({'factors':[2,600],'total':1200}))
        self.assertFalse(Relation.parse('product(factors) == total').evaluate({'factors':[2,600],'total':600}))

    def test_shared_maps_are_deduplicated_and_walked_evidence_wins(self):
        graph=AppGraph(base_url='https://example.test')
        source=graph.observe({'url':'https://example.test/','elements':[]})
        target=graph.observe({'url':'https://example.test/done','elements':[]})
        action=Action(source.id,target.id,'link','Next',inferred=True)
        graph.connect(action)
        other=AppGraph.from_dict(graph.to_dict())
        other.connect(replace(action,inferred=False))
        merged=merge_graphs([graph,other,other])
        self.assertEqual(len(merged.states),2)
        self.assertEqual(len(merged.actions),1)
        self.assertFalse(merged.actions[0].inferred)
        merged.metrics['queued_actions']=1
        self.assertFalse(merged.is_closed())


class CrawlCheckpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=server();cls.thread=threading.Thread(target=cls.app.serve_forever,daemon=True);cls.thread.start()
        cls.base=f'http://127.0.0.1:{cls.app.server_port}'
        cls.p=sync_playwright().start();cls.browser=cls.p.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.p.stop();cls.app.shutdown();cls.app.server_close();cls.thread.join()

    def setUp(self):
        self.page=self.browser.new_page()

    def tearDown(self):
        self.page.context.close()

    def config(self, **kwargs):
        return CrawlConfig(identity_reads=1,settle_ms=10,timeout_ms=800,max_states=100,
                           max_actions=1000,max_attempts=1000,max_seconds=30,**kwargs)

    def test_mutating_url_restores_empty_cart_by_replaying_removal(self):
        crawler=Crawler(self.page,self.base,self.config())
        try:
            graph=crawler.crawl('/sticky?add=1')
            self.assertTrue(any(s.url_template=='/sticky-done' for s in graph.states.values()))
            self.assertGreaterEqual(graph.metrics['recipe_restores'],1)
            self.assertEqual(graph.metrics['restore_failures'],0)
            self.assertTrue(graph.is_closed(),graph.to_dict())
        finally:
            crawler.close()

    def test_interrupted_crawl_resumes_in_fresh_browser_and_preserves_active_task(self):
        class Interrupted(Crawler):
            def _explore(self,item):
                if self._attempts==2:
                    raise KeyboardInterrupt()
                super()._explore(item)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'crawl.json'
            crawler=Interrupted(self.page,self.base,self.config(checkpoint_path=str(path)))
            try:
                with self.assertRaises(KeyboardInterrupt):crawler.crawl('/sticky?add=1')
            finally:
                crawler.close()
            saved=json.loads(path.read_text())
            self.assertIsNotNone(saved['active'])
            self.assertEqual(os.stat(path).st_mode & 0o777,0o600)
            self.assertEqual(os.stat(str(path)+'.data').st_mode & 0o777,0o700)
            fresh=self.browser.new_page()
            resumed=Crawler(fresh,self.base,self.config(checkpoint_path=str(path),resume=True))
            try:
                graph=resumed.crawl('/sticky?add=1')
                self.assertTrue(graph.is_closed(),graph.to_dict())
                self.assertEqual(graph.metrics['stop_reason'],'exhausted')
                self.assertGreater(len(resumed.readables),1)
                self.assertIsNone(json.loads(path.read_text())['active'])
                self.assertFalse(json.loads(path.read_text())['frontier'])
            finally:
                resumed.close();fresh.context.close()
            incompatible=Crawler(self.page,self.base,self.config(checkpoint_path=str(path),resume=True,explore_inputs=False))
            with self.assertRaisesRegex(ValueError,'policy differs'):
                incompatible.crawl('/sticky?add=1')
            manifest=json.loads(path.read_text())
            node=Path(str(path)+'.data')/(manifest['graph']['states'][0]['ref']+'.json')
            data=json.loads(node.read_text());data['state']['title']='tampered'
            node.write_text(json.dumps(data))
            corrupted=Crawler(self.page,self.base,self.config(checkpoint_path=str(path),resume=True))
            with self.assertRaisesRegex(ValueError,'integrity'):
                corrupted.crawl('/sticky?add=1')

    def test_control_choices_produce_replayable_branches_with_continuations(self):
        crawler=Crawler(self.page,self.base,self.config())
        try:
            graph=crawler.crawl('/choices')
            selections=[a for a in graph.actions if a.kind=='select']
            self.assertTrue(any(a.value=='express' for a in selections))
            selected=next(a for a in selections if a.value=='express')
            steps=path_steps(graph,graph.path_to(selected.target))
            self.assertTrue(any(s.kind=='select' and s.value=='express' for s in steps))
            self.assertTrue(any(a.source==selected.target and a.kind=='click' and
                graph.states[a.target].url_template.startswith('/choices-done') for a in graph.actions))
            self.assertTrue(any(a.kind=='check' for a in graph.actions))
            self.assertTrue(graph.is_closed(),graph.to_dict())
        finally:
            crawler.close()

    def test_resume_budget_is_per_run_and_can_retry_failed_restoration(self):
        class CannotRestore(Crawler):
            def _establish(self,state):
                return False
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'retry.json'
            config=replace(self.config(checkpoint_path=str(path)),max_attempts=1)
            failed=CannotRestore(self.page,self.base,config)
            try:
                graph=failed.crawl('/sticky?add=1')
                self.assertFalse(graph.is_closed())
                self.assertEqual(graph.metrics['stop_reason'],'max_attempts')
            finally:
                failed.close()
            resumed=Crawler(self.page,self.base,replace(config,resume=True,retry_failed=True,max_attempts=1000))
            try:
                graph=resumed.crawl('/sticky?add=1')
                self.assertTrue(graph.is_closed(),graph.to_dict())
                self.assertFalse(any(t['status']=='restoration_failed' for t in resumed._task_results.values()))
                self.assertGreater(graph.metrics['attempted_actions'],1)
            finally:
                resumed.close()

    def test_cli_budgeted_discovery_can_resume_into_a_new_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);journal=root/'crawl.json'
            shared=[sys.executable,'main.py',self.base,'--discover-only','--entry','/sticky?add=1',
                    '--identity-reads','1','--settle','10','--timeout','1000','--runs-dir',str(root/'runs')]
            first=subprocess.run(shared+['--baseline-dir',str(root/'partial'),'--max-attempts','1',
                                         '--crawl-checkpoint',str(journal)],capture_output=True,text=True,timeout=30)
            self.assertEqual(first.returncode,0,first.stdout+first.stderr)
            self.assertTrue(json.loads(journal.read_text())['frontier'])
            second=subprocess.run(shared+['--baseline-dir',str(root/'complete'),'--resume-crawl',str(journal)],
                                  capture_output=True,text=True,timeout=30)
            self.assertEqual(second.returncode,0,second.stdout+second.stderr)
            saved=json.loads(journal.read_text())
            self.assertFalse(saved['frontier'])
            self.assertEqual(saved['graph']['metrics']['stop_reason'],'exhausted')

    def test_depth_limited_actions_remain_gaps_and_resume_with_larger_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=replace(self.config(checkpoint_path=str(Path(tmp)/'depth.json')),max_depth=1)
            crawler=Crawler(self.page,self.base,config)
            try:
                graph=crawler.crawl('/depth-a')
                self.assertFalse(graph.is_closed())
                self.assertGreater(graph.metrics['task_statuses'].get('depth_limited',0),0)
            finally:crawler.close()
            crawler=Crawler(self.page,self.base,replace(config,resume=True,max_depth=0))
            try:
                graph=crawler.crawl('/depth-a')
                self.assertTrue(graph.is_closed(),graph.to_dict())
                self.assertTrue(any(s.url_template=='/depth-d' for s in graph.states.values()))
            finally:crawler.close()

    def test_interruption_before_initial_navigation_restarts_entry_on_resume(self):
        class EarlyInterrupt(Crawler):
            def _reset(self):
                raise KeyboardInterrupt()
        with tempfile.TemporaryDirectory() as tmp:
            config=self.config(checkpoint_path=str(Path(tmp)/'early.json'))
            crawler=EarlyInterrupt(self.page,self.base,config)
            with self.assertRaises(KeyboardInterrupt):crawler.crawl('/sticky?add=1')
            crawler=Crawler(self.page,self.base,replace(config,resume=True))
            try:
                graph=crawler.crawl('/sticky?add=1')
                self.assertTrue(graph.entry)
                self.assertTrue(graph.is_closed())
            finally:crawler.close()

    def test_numeric_control_bindings_use_values_instead_of_option_labels(self):
        from runner.flow_runner import read_binding
        from runner.persistence_runner import StepError
        self.page.goto(self.base+'/quantity')
        self.assertEqual(read_binding(self.page,BindingSpec('qty','#quantity','numeric_value'),1000),1)
        factors=read_binding(self.page,BindingSpec('factors','#quantity,.amount','numeric_values'),1000)
        self.assertEqual(factors,[1,600])
        self.page.locator('#quantity').fill('abc')
        with self.assertRaises(StepError):
            read_binding(self.page,BindingSpec('qty','#quantity','numeric_value'),1000)

    def test_async_quantity_change_waits_for_explicit_readiness(self):
        action=PathStep('select','Quantity',{'selector':'#qty'},'2',
                        ({'selector':'#loading','kind':'count','expected':0},))
        self.assertEqual(PathStep.from_dict(action.to_dict()),action)
        instance=self.instance('/async-choice',[action],'product(factors) == total',
                               BindingSpec('factors','#qty,#unit','numeric_values'))
        instance=replace(instance,bindings=(*instance.bindings,BindingSpec('total','#total','number')))
        result=FlowRunner(self.page,timeout_ms=1000,settle_ms=0).run(instance)
        record=flow_test_record(instance,result,evaluate_instance(instance,result))
        self.assertEqual(record.expectation_status,'holds',record.to_dict())
        self.assertEqual(record.observations['total'],1200)

    def instance(self, entry, goal, relation, binding, **contract_fields):
        self.page.goto(self.base+entry);self.page.wait_for_timeout(50)
        state=AppGraph(base_url=self.base).observe(inspect_with_page(self.page))
        contract=BehaviorContract(capability='persistent_mutation',invariant='LIVE_GAP',
            expected_relation=relation,precondition='goal_reached == true',goal_path=entry.split('?')[0],
            ready=[{'selector':'body','kind':'count','expected':1}],**contract_fields)
        return FlowTestInstance('gap','LIVE_GAP',contract.capability,self.base,self.base+entry,
            state.id,state.id,(),tuple(goal),(binding,),None,'',
            ('walk_setup','walk_goal','observe_bindings'),('reset_session',),relation,
            'goal_reached == true',contract=contract.model_dump(mode='json'))

    def test_removal_persistence_is_checked_after_reload(self):
        instance=self.instance('/sticky?add=1',[PathStep('click','Remove',{'selector':'#remove'})],
            'rows == 0',BindingSpec('rows','#remove','count'),
            reload_checks=[{'id':'REMOVAL_PERSISTENCE','relation':'rows == 0',
                'bindings':[{'name':'rows','selector':'#remove','extract':'count'}]}])
        result=FlowRunner(self.page,timeout_ms=1000,settle_ms=20).run(instance)
        record=flow_test_record(instance,result,evaluate_instance(instance,result))
        self.assertEqual(record.status,'holds')
        self.assertEqual(record.expectation_status,'violated')
        self.assertEqual(next(o['status'] for o in record.outcomes if o['id']=='RELOAD/REMOVAL_PERSISTENCE'),'violated')

    def test_initial_load_errors_fail_and_explicit_scoped_rules_remain_recorded(self):
        instance=self.instance('/broken/admin',[],'body_count == 1',BindingSpec('body_count','body','count'))
        result=FlowRunner(self.page,timeout_ms=1000,settle_ms=20).run(instance)
        record=flow_test_record(instance,result,evaluate_instance(instance,result))
        self.assertEqual(record.expectation_status,'violated')
        self.assertTrue(result.notes['runtime_errors'])
        contract={**instance.contract,'runtime_error_rules':[{'pattern':'Cannot read properties',
            'reason':'controlled fixture validates explicit record-only policy','phases':['fixture','setup','goal']}]}
        configured=replace(instance,contract=contract)
        result=FlowRunner(self.page,timeout_ms=1000,settle_ms=20).run(configured)
        record=flow_test_record(configured,result,evaluate_instance(configured,result))
        self.assertEqual(record.expectation_status,'holds')
        self.assertTrue(all(e.get('record_only_reason') for e in result.notes['runtime_errors']))
