"""Strict readiness values and representation-safe generated checks; no API calls."""
import copy
import json
import unittest

from knowledge.contracts import UIAssertion
from regression.expectations import ExpectedBehavior,ExpectationSuite
from test_ai_expectations import expected


class ReadinessValidationTests(unittest.TestCase):
    def test_boolean_kinds_reject_null_strings_and_numbers(self):
        for kind in ('visible','checked','enabled'):
            for value in (None,'true','Shopping Cart',0,1,0.0,[],{}):
                with self.subTest(kind=kind,value=value),self.assertRaises(ValueError):
                    UIAssertion(selector='#target',kind=kind,expected=value)
            for value in (True,False):
                self.assertIs(UIAssertion(selector='#target',kind=kind,expected=value).expected,value)
    def test_counts_require_nonnegative_integers_without_coercion(self):
        for value in (None,True,False,-1,1.0,'1',[],{}):
            with self.subTest(value=value),self.assertRaises(ValueError):
                UIAssertion(selector='#target',kind='count',expected=value)
        for value in (0,1,5):self.assertEqual(UIAssertion(selector='#target',kind='count',expected=value).expected,value)
    def test_text_and_values_require_strings(self):
        for kind in ('text','value'):
            for value in (None,True,0,['text'],{}):
                with self.subTest(kind=kind,value=value),self.assertRaises(ValueError):
                    UIAssertion(selector='#target',kind=kind,expected=value)
            for value in ('','Shopping Cart'):
                self.assertEqual(UIAssertion(selector='#target',kind=kind,expected=value).expected,value)
    def test_generated_readiness_uses_same_strict_validation(self):
        for bad in (None,'Shopping Cart',1):
            data=expected('source','goal')
            data['observations'][1]['ready']=[dict(selector='#quantity',kind='visible',expected=bad)]
            with self.assertRaises(ValueError):ExpectedBehavior.model_validate(data)
        data['observations'][1]['ready'][0]['expected']=True
        self.assertTrue(ExpectedBehavior.model_validate(data).observations[1].ready[0].expected)
    def test_defaults_are_valid_only_for_boolean_readiness(self):
        self.assertTrue(UIAssertion(selector='#target').expected)
        for kind in ('count','text','value'):
            with self.assertRaises(ValueError):UIAssertion(selector='#target',kind=kind)


class ComparisonValidationTests(unittest.TestCase):
    def behavior(self,relation,left,right):
        from knowledge.engine import Relation
        data=expected('source','goal');data['relation']=relation
        for point,extract in zip(data['observations'],(left,right)):
            point['binding']['extract']=extract
        names=Relation.parse(relation).operands
        data['observations']=[p for p in data['observations'] if p['binding']['name'] in names]
        return ExpectedBehavior.model_validate(data)
    def test_rejects_incompatible_scalar_list_and_boolean_comparisons(self):
        for left,right in (('texts','text'),('text','number'),('numbers','texts'),('count','checked'),('value','numeric_value')):
            with self.subTest(left=left,right=right),self.assertRaisesRegex(ValueError,'incompatible comparison'):
                self.behavior('chosen == cart',left,right)
    def test_compatible_representations_and_numeric_reductions_still_work(self):
        for left,right in (('text','value'),('selected','text'),('numeric_value','number'),('texts','texts'),('numbers','numeric_values'),('enabled','checked')):
            self.behavior('chosen == cart',left,right)
        for relation,left,right in (
            ('sum(chosen) == cart','numbers','numeric_value'),
            ('product(chosen) == cart','numeric_values','number'),
            ('count(chosen) == cart','texts','count'),
            ('len(chosen) == cart','text','number'),
            ('positive_integers(chosen) == true','value','number'),
            ('finite(chosen) == true','texts','number'),
            ('sorted_asc(chosen) == true','numbers','number'),
            ('no_errors(chosen) == true','texts','number')):
            with self.subTest(relation=relation):self.behavior(relation,left,right)
    def test_function_input_types_are_checked(self):
        for relation,left in (('sum(chosen) == cart','texts'),('count(chosen) == cart','count'),
                              ('sorted_asc(chosen) == true','text'),('finite(chosen) == true','checked'),
                              ('no_errors(chosen) == true','number')):
            with self.subTest(relation=relation),self.assertRaisesRegex(ValueError,'cannot consume'):
                self.behavior(relation,left,'number')
    def test_ordering_requires_numbers(self):
        for kind in ('text','texts','visible'):
            with self.subTest(kind=kind),self.assertRaisesRegex(ValueError,'numeric'):
                self.behavior('chosen > cart',kind,kind)
        self.behavior('chosen >= cart','number','numeric_value')
    def test_saved_bad_checks_become_gaps_without_hiding_valid_checks(self):
        good=expected('source','goal');bad=copy.deepcopy(good);bad['id']='bad_ready'
        bad['observations'][1]['ready']=[dict(selector='#quantity',kind='visible',expected=None)]
        mismatch=copy.deepcopy(good);mismatch['id']='bad_types';mismatch['observations'][0]['binding']['extract']='texts'
        suite=ExpectationSuite.from_saved_json(json.dumps(dict(model='saved-model',flow_hashes={'checkout':'hash'},
            expectations=[good,bad,mismatch],gaps=[])))
        self.assertEqual([e.id for e in suite.expectations],['cart_quantity'])
        self.assertEqual({g.expectation_id for g in suite.gaps},{'bad_ready','bad_types'})
        self.assertTrue(any('boolean' in g.reason for g in suite.gaps))
        self.assertTrue(any('incompatible comparison' in g.reason for g in suite.gaps))
