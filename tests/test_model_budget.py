"""No network calls: spending boundaries, crash recovery, and per-flow resume."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from semantic.budget import ModelBudget, ModelBudgetError

REQUEST = dict(system='Generate checks.',user='flow one',schema={'type':'object','properties':{}})


class Backend:
    model = 'fake'
    def __init__(self): self.calls=0
    def complete_json(self, **kwargs):
        self.calls+=1
        return '{"expectations":[],"gaps":[]}'


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'budget.sqlite3'
    def tearDown(self): self.tmp.cleanup()
    def budget(self,**kwargs):return ModelBudget(self.path,**kwargs)
    def test_shared_limit_across_stages_and_response_reuse_after_restart(self):
        budget=self.budget(max_calls=1);first=Backend();second=Backend()
        answer=budget.wrap(first).complete_json(**REQUEST)
        with self.assertRaises(ModelBudgetError):
            budget.wrap(second).complete_json(**{**REQUEST,'user':'classification two'})
        restarted=self.budget(max_calls=0)
        self.assertEqual(restarted.wrap(second).complete_json(**REQUEST),answer)
        self.assertEqual(first.calls,1);self.assertEqual(second.calls,0)
        self.assertEqual(restarted.summary()['attempted_calls'],1)
    def test_failed_and_interrupted_requests_are_charged_and_not_retried(self):
        for error in (RuntimeError,KeyboardInterrupt):
            backend=Backend();backend.complete_json=Mock(side_effect=error('private error'))
            budget=self.budget(max_calls=5)
            request={**REQUEST,'user':error.__name__}
            with self.assertRaises(error):budget.wrap(backend).complete_json(**request)
            with self.assertRaises(ModelBudgetError):self.budget().wrap(backend).complete_json(**request)
            self.assertEqual(backend.complete_json.call_count,1)
        self.assertEqual(self.budget().summary()['failed_calls'],2)
        self.assertNotIn(b'private error',self.path.read_bytes())
    def test_pending_request_after_process_loss_never_reissues(self):
        budget=self.budget();backend=Backend();budget.wrap(backend).complete_json(**REQUEST)
        with budget.connect() as db:
            db.execute("UPDATE requests SET status='pending',response=NULL")
        with self.assertRaises(ModelBudgetError):self.budget().wrap(backend).complete_json(**REQUEST)
        self.assertEqual(backend.calls,1)
    def test_token_and_input_limits_block_before_network(self):
        for limits in ({'max_tokens':1},{'max_input_tokens':1},{'max_calls':0}):
            backend=Backend()
            with self.assertRaises(ModelBudgetError):self.budget(**limits).wrap(backend).complete_json(**REQUEST)
            self.assertEqual(backend.calls,0)
        self.assertEqual(self.budget().summary()['attempted_calls'],0)
    def test_reservations_are_transactional_under_concurrency(self):
        budgets=[self.budget(max_calls=1),self.budget(max_calls=1)]
        def call(index):
            try:
                return budgets[index].wrap(Backend()).complete_json(**{**REQUEST,'user':str(index)})
            except ModelBudgetError:return 'blocked'
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(call,range(2)))
        self.assertEqual(results.count('blocked'),1)
        self.assertEqual(self.budget().summary()['attempted_calls'],1)
    def test_explicit_larger_budget_continues_without_repeating_completed_calls(self):
        backend=Backend();small=self.budget(max_calls=1);small.wrap(backend).complete_json(**REQUEST)
        larger=self.budget(max_calls=2)
        larger.wrap(backend).complete_json(**REQUEST)
        larger.wrap(backend).complete_json(**{**REQUEST,'user':'next flow'})
        self.assertEqual(backend.calls,2)
    def test_provider_or_evidence_changes_do_not_reuse_response(self):
        backend=Backend();budget=self.budget()
        budget.wrap(backend,provider='a').complete_json(**REQUEST)
        budget.wrap(backend,provider='b').complete_json(**REQUEST)
        budget.wrap(backend,provider='a').complete_json(**{**REQUEST,'user':'changed evidence'})
        self.assertEqual(backend.calls,3)
    def test_builtin_adapters_disable_retries_and_bound_output(self):
        from semantic.classifier import OpenAIBackend,AnthropicBackend
        with patch('openai.OpenAI') as factory:
            backend=OpenAIBackend(max_tokens=123)
            factory.return_value.chat.completions.create.return_value=SimpleNamespace(
                choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='{}',refusal=None))],usage=None)
            backend.complete_json(**REQUEST)
            factory.assert_called_once_with(max_retries=0)
            self.assertEqual(factory.return_value.chat.completions.create.call_args.kwargs['max_completion_tokens'],123)
        with patch('anthropic.Anthropic') as factory:
            backend=AnthropicBackend(max_tokens=123)
            factory.return_value.messages.create.return_value=SimpleNamespace(stop_reason='end_turn',
                usage=None,content=[SimpleNamespace(type='text',text='{}')])
            backend.complete_json(**REQUEST)
            factory.assert_called_once_with(max_retries=0)
            self.assertEqual(factory.return_value.messages.create.call_args.kwargs['max_tokens'],123)
    def test_cli_limits_are_shared_and_negative_limits_rejected(self):
        from main import parse_args,_model_backend
        args=parse_args(['http://localhost','--runs-dir',self.tmp.name,'--max-model-calls','1'])
        with patch('main.backend_for',side_effect=lambda *a,**kw:Backend()):
            _model_backend(args).complete_json(**REQUEST)
            with self.assertRaises(ModelBudgetError):
                _model_backend(args,default_provider='openai').complete_json(**{**REQUEST,'user':'different stage'})
        for flag in ('--max-model-calls','--max-model-tokens','--max-model-input-tokens','--max-model-output-tokens'):
            with patch('sys.stderr'),self.assertRaises(SystemExit):parse_args(['http://localhost',flag,'-1'])


class GenerationResumeTests(unittest.TestCase):
    def test_each_flow_saved_and_resumed_from_cached_responses(self):
        from discovery.graph import AppGraph,Action
        from regression.expectations import IdentifiedFlow,generate_expectations,compile_expectations
        graph=AppGraph(base_url='http://localhost')
        source=graph.observe({'url':'http://localhost/product','elements':[{'tag':'a','text':'Next','selector':'#next','href':'/cart'}]})
        goal=graph.observe({'url':'http://localhost/cart','elements':[{'tag':'input','type':'number','selector':'#qty'}]})
        graph.entry=source.id;aff=next(iter(source.affordances.values()))
        graph.actions.append(Action(source.id,goal.id,aff.key,aff.label))
        flows=[IdentifiedFlow(id=str(i),name='Cart '+str(i),setup_state_id=source.id,
            goal_state_id=goal.id,evidence='Observed product to cart') for i in range(3)]
        class FlowBackend(Backend):
            def complete_json(self,**kwargs):
                self.calls+=1;fid=json.loads(kwargs['user'])['flow']['id']
                return json.dumps({'expectations':[], 'gaps':[{'flow_id':fid,'expectation_id':None,'reason':'Insufficient evidence'}]})
        with tempfile.TemporaryDirectory() as tmp:
            journal=Path(tmp)/'calls.sqlite3';progress=Path(tmp)/'progress.json';backend=FlowBackend()
            suite=generate_expectations(flows,graph,{},ModelBudget(journal,max_calls=1).wrap(backend),checkpoint=progress)
            self.assertEqual(backend.calls,1);self.assertEqual(len(suite.gaps),3)
            self.assertIn('budget exhausted',suite.gaps[1].reason)
            self.assertEqual(json.loads(progress.read_text())['flow_hashes'],suite.flow_hashes)
            # Changing operational timing cannot trigger fresh generation.
            graph.metrics['elapsed_seconds']=999
            resumed=generate_expectations(flows,graph,{},ModelBudget(journal,max_calls=3).wrap(backend),checkpoint=progress)
            self.assertEqual(backend.calls,3)
            self.assertEqual([g.reason for g in resumed.gaps],['Insufficient evidence']*3)
            self.assertFalse(compile_expectations(resumed,flows,graph,{})[0])
