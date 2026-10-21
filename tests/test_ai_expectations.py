"""AI output validation and real-browser execution with a deterministic model stub."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from discovery.graph import AppGraph, Action
from discovery.page_inspector import inspect_with_page
from discovery.readable import readable_with_page
from regression.expectations import (IdentifiedFlow, ExpectedBehavior, generate_expectations,
    compile_expectations, flow_packet)
from knowledge.flows import FlowTestInstance, evaluate_instance
from runner.flow_runner import FlowRunner
from baseline import saver, loader
from regression.results import signature


class Model:
    model = 'deterministic-expectation-model'
    def __init__(self, answer):
        self.answer = answer
        self.packets = []
    def complete_json(self, *, system, user, schema):
        self.packets.append(json.loads(user))
        assert 'Do not reclassify' in system
        assert schema
        return json.dumps(self.answer)


def expected(source, goal):
    return dict(id='cart_quantity', flow_id='checkout', description='Preserve selected quantity',
        expected_outcome='Cart contains the selected quantity', relation='chosen == cart',
        observations=[dict(phase='goal', after_step=0, state_id=source,
            binding=dict(name='chosen',selector='#quantity',extract='numeric_value'),ready=[]),
            dict(phase='goal',after_step=1,state_id=goal,
            binding=dict(name='cart',selector='#quantity',extract='numeric_value'),ready=[])],
        evidence=[dict(state_id=source,selector='#quantity',explanation='Product quantity control'),
                  dict(state_id=goal,selector='#quantity',explanation='Cart quantity control')],
        assumptions=[],confidence=.9,support='supported',uncertainty_reason=None,severity='high')


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_GET(self):
        broken = self.path.startswith('/broken')
        prefix = '/broken' if broken else ''
        product = self.path.endswith('/product')
        qty = 2 if product or not broken else 1
        body = '<h1>'+('Product' if product else 'Cart')+'</h1>'
        body += f'<input id="quantity" type="number" value="{qty}">'
        if product:
            body += f'<a id="add" href="{prefix}/cart">Add to cart</a>'
        self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers()
        self.wfile.write(body.encode())


class AIExpectations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base=f'http://127.0.0.1:{cls.server.server_port}'
        cls.p=sync_playwright().start();cls.browser=cls.p.chromium.launch()
    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.p.stop();cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def setUp(self):
        self.page=self.browser.new_page()
        self.graph=AppGraph(base_url=self.base);self.readables={}
        states=[]
        for path in ('/product','/cart'):
            self.page.goto(self.base+path)
            state=self.graph.observe(inspect_with_page(self.page));states.append(state)
            self.readables[state.id]=readable_with_page(self.page)
        self.source,self.goal=states;self.graph.entry=self.source.id
        aff=next(a for a in self.source.affordances.values() if a.kind=='link')
        self.graph.actions.append(Action(self.source.id,self.goal.id,aff.key,aff.label))
        self.flow=IdentifiedFlow(id='checkout',name='Add product to cart',setup_state_id=self.source.id,
            goal_state_id=self.goal.id,evidence='Observed Add to cart transition')
        self.answer=dict(expectations=[expected(self.source.id,self.goal.id)],gaps=[])
    def tearDown(self): self.page.close()
    def generate(self, answer=None):
        self.model=Model(answer or self.answer)
        return generate_expectations([self.flow],self.graph,self.readables,self.model)
    def test_generation_browser_replay_and_serialization(self):
        suite=self.generate()
        self.assertEqual(self.model.packets[0]['flow']['name'],'Add product to cart')
        self.assertTrue(self.model.packets[0]['states'])
        instances,gaps=compile_expectations(suite,[self.flow],self.graph,self.readables)
        self.assertFalse(gaps);self.assertEqual(len(instances),1)
        instance=instances[0]
        self.assertEqual(instance.contract['review_status'],'inferred')
        self.assertEqual(instance.expected_relation,'chosen == cart')
        self.assertEqual(FlowTestInstance.from_dict(instance.to_dict()),instance)
        changed=copy.deepcopy(instance.to_dict());changed['observation_points'][0]['after_step']=1
        self.assertNotEqual(signature(instance.to_dict()),signature(changed))
        runner=FlowRunner(self.page,timeout_ms=1200,settle_ms=0)
        good=runner.run(instance);verdict=evaluate_instance(instance,good)
        self.assertEqual(verdict.status.value,'holds',str((good.error,good.notes,verdict)))
        with tempfile.TemporaryDirectory() as tmp:
            record=saver.build(graph=self.graph,tests=[saver.flow_test_record(instance,good,verdict)],
                readables=self.readables,coverage={'expectation_suite':suite.model_dump(mode='json')})
            saver.save(record,Path(tmp)/'baseline')
            self.assertTrue((Path(tmp)/'baseline/expectations.json').exists())
            saved=loader.load(Path(tmp)/'baseline');_,frozen=next(iter(loader.instances(saved)))
            broken=loader.retarget_instance(frozen,self.base,self.base+'/broken')
            result=runner.run(broken);bad=evaluate_instance(broken,result)
            self.assertEqual(bad.status.value,'violated',str((result.error,result.notes,bad)))
            self.assertEqual(result.observations['chosen'],2)
            self.assertEqual(result.observations['cart'],1)
            self.assertEqual(len(result.notes['observation_evidence']),2)
            from regression.differ import diff
            from regression.findings import Direction
            current=saver.build(graph=AppGraph(base_url=self.base+'/broken'),
                tests=[saver.flow_test_record(broken,result,bad)])
            found=diff(saved,current,include_structure=False,include_ui=False)
            self.assertTrue(found.of_direction(Direction.SUSPECTED))
            self.assertFalse(found.regressions)
    def test_invalid_selector_and_timeline_are_gaps(self):
        for mutate in (lambda e:e['observations'][0]['binding'].update(selector='#invented'),
                       lambda e:e['observations'][0].update(after_step=99)):
            answer=copy.deepcopy(self.answer);mutate(answer['expectations'][0])
            suite=self.generate(answer)
            self.assertFalse(suite.expectations);self.assertTrue(suite.gaps)
    def test_unsafe_expressions_and_reserved_observations_rejected(self):
        for relation in ('chosen == chosen','eval(chosen) == cart','chosen + 1 == cart'):
            data=copy.deepcopy(self.answer['expectations'][0]);data['relation']=relation
            with self.assertRaises(ValueError):ExpectedBehavior.model_validate(data)
        data=copy.deepcopy(self.answer['expectations'][0]);data['relation']='goal_reached == cart'
        data['observations'][0]['binding']['name']='goal_reached'
        with self.assertRaises(ValueError):ExpectedBehavior.model_validate(data)
    def test_uncertainty_and_empty_response_are_explicit(self):
        answer=copy.deepcopy(self.answer);answer['expectations'][0].update(support='uncertain',uncertainty_reason='Missing business requirements')
        suite=self.generate(answer);instances,gaps=compile_expectations(suite,[self.flow],self.graph,self.readables)
        self.assertFalse(instances);self.assertIn('Missing business',gaps[0].unmet[0])
        suite=self.generate(dict(expectations=[],gaps=[]));self.assertTrue(suite.gaps)
    def test_cache_rejects_changed_evidence_but_ignores_timing(self):
        suite=self.generate();self.graph.metrics['elapsed_seconds']=987
        self.assertTrue(compile_expectations(suite,[self.flow],self.graph,self.readables)[0])
        self.flow.evidence='Different flow evidence'
        instances,gaps=compile_expectations(suite,[self.flow],self.graph,self.readables)
        self.assertFalse(instances);self.assertIn('regenerate',gaps[0].unmet[0])
    def test_inferred_paths_never_become_executable_checks(self):
        from dataclasses import replace
        self.graph.actions[0]=replace(self.graph.actions[0],inferred=True)
        suite=self.generate();self.assertFalse(self.model.packets);self.assertTrue(suite.gaps)
    def test_cli_uses_saved_suite_without_model_call(self):
        from main import parse_args,_run_flows
        from reporting.reporter import ConsoleReporter
        suite=self.generate()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'suite.json';path.write_text(suite.model_dump_json())
            args=parse_args([self.base,'--baseline','--expectations',str(path),'--runs-dir',tmp])
            with patch('main.backend_for',side_effect=AssertionError('Unexpected model call')):
                entries,records,verdicts,gaps=_run_flows(self.page,self.graph,[self.flow],args,ConsoleReporter(),self.readables)
            self.assertEqual(len(records),1);self.assertFalse(gaps)
            self.assertEqual(entries[0]['generated_expectation']['model'],Model.model)
    def test_cli_generation_and_reload_capture(self):
        from main import parse_args,_run_flows
        from reporting.reporter import ConsoleReporter
        import io
        data=copy.deepcopy(self.answer)
        data['expectations'][0]['observations'][1].update(phase='reload',after_step=0)
        model=Model(data)
        with tempfile.TemporaryDirectory() as tmp:
            args=parse_args([self.base,'--baseline','--runs-dir',tmp])
            with patch('main.backend_for',return_value=model):
                entries,records,verdicts,gaps=_run_flows(self.page,self.graph,[self.flow],args,ConsoleReporter(stream=io.StringIO()),self.readables)
            self.assertEqual(len(model.packets),1);self.assertFalse(gaps)
            self.assertEqual(verdicts[0].status.value,'holds')
            self.assertEqual(entries[0]['observation_evidence'][1]['phase'],'reload')
            self.assertTrue(list((Path(tmp)/'expectations').glob('*.json')))
    def test_partial_invalid_output_does_not_hide_valid_check(self):
        data=copy.deepcopy(self.answer);invalid=copy.deepcopy(data['expectations'][0])
        invalid.update(id='invalid',relation='eval(chosen) == cart')
        data['expectations'].append(invalid)
        suite=self.generate(data)
        instances,gaps=compile_expectations(suite,[self.flow],self.graph,self.readables)
        self.assertEqual(len(instances),1);self.assertEqual(len(gaps),1)
        suite.expectations=[];suite.gaps=[]
        instances,gaps=compile_expectations(suite,[self.flow],self.graph,self.readables)
        self.assertFalse(instances);self.assertTrue(gaps)
    def test_low_confidence_and_failed_observation_are_not_passes(self):
        suite=self.generate();suite.expectations[0].confidence=.4
        instances,gaps=compile_expectations(suite,[self.flow],self.graph,self.readables)
        self.assertFalse(instances);self.assertTrue(gaps)
        suite.expectations[0].confidence=.9
        instance=compile_expectations(suite,[self.flow],self.graph,self.readables)[0][0]
        result=FlowRunner(self.page,timeout_ms=1200,settle_ms=0).run(instance)
        result.notes['observation_errors']=[{'detail':'observation failed'}]
        self.assertEqual(evaluate_instance(instance,result).status.value,'inconclusive')

class ProviderSchemaTests(unittest.TestCase):
    def test_generation_schema_has_typed_required_properties(self):
        from regression.expectations import GenerationOutput
        from semantic.schemas import strict_schema
        schema = strict_schema(GenerationOutput)
        def check(node):
            if isinstance(node, dict):
                self.assertNotIn('default', node)
                if '$ref' in node:
                    self.assertEqual(set(node), {'$ref'})
                if node.get('type') == 'object':
                    self.assertIs(node['additionalProperties'], False)
                    self.assertEqual(set(node['required']), set(node['properties']))
                    for value in node['properties'].values():
                        self.assertTrue(any(k in value for k in ('type', '$ref', 'anyOf', 'enum')))
                for value in node.values():check(value)
            elif isinstance(node, list):
                for value in node:check(value)
        check(schema)
