"""Combine discovery maps without counting shared states or edges twice."""
import argparse
import json
from copy import deepcopy
from pathlib import Path
from discovery.graph import AppGraph


def merge_graphs(graphs):
    graphs = list(graphs)
    if not graphs:
        raise ValueError('at least one graph is required')
    merged = AppGraph(base_url=graphs[0].base_url, entry=graphs[0].entry)
    policy = [(g.metrics.get('id_segment_patterns', []),g.metrics.get('input_states',False),
               sorted((key,sorted(value)) for key,value in g.volatile.items())) for g in graphs]
    if any(g.base_url!=merged.base_url for g in graphs) or any(p!=policy[0] for p in policy):
        raise ValueError('maps must have the same target and identity policy')
    for graph in graphs:
        for sid,state in graph.states.items():
            if sid in merged.states:
                if merged.states[sid].signature!=state.signature or merged.states[sid].url_template!=state.url_template:
                    raise ValueError(f'inconsistent state identity: {sid}')
                merged.states[sid].affordances.update(deepcopy(state.affordances))
            else:
                merged.states[sid] = deepcopy(state)
        for action in graph.actions:
            merged.connect(deepcopy(action))
        for attribute in ('unexplored','out_of_region'):
            for sid,keys in getattr(graph,attribute).items():
                values = getattr(merged,attribute).setdefault(sid,[])
                values.extend(key for key in keys if key not in values)
        for template,keys in graph.volatile.items():
            merged.volatile.setdefault(template,[])
            merged.volatile[template] = sorted(set(merged.volatile[template]) | set(keys))
    merged.metrics = {'id_segment_patterns':policy[0][0], 'input_states':policy[0][1],
        'walked_transitions':sum(not a.inferred for a in merged.actions),
        'inferred_transitions':sum(a.inferred for a in merged.actions),
        'queued_actions':sum(g.metrics.get('queued_actions',0) for g in graphs),
        'unsupported_frames':sum(g.metrics.get('unsupported_frames',0) for g in graphs),
        'queue_count_scope':'sum of source counts; shared queues cannot be deduplicated from graphs alone',
        'source_runs':[g.metrics for g in graphs]}
    return merged


def summary(graph):
    return {'states':len(graph.states),'transitions':len(graph.actions),
        'walked_transitions':sum(not a.inferred for a in graph.actions),
        'inferred_transitions':sum(a.inferred for a in graph.actions),
        'unresolved_actions':sum(map(len,graph.unexplored.values())),
        'outside_region':sum(map(len,graph.out_of_region.values())),
        'closed':graph.is_closed(),'metrics':graph.metrics}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('graphs',nargs='+',type=Path)
    parser.add_argument('--out',type=Path)
    args=parser.parse_args()
    graph=merge_graphs(AppGraph.load(path) for path in args.graphs)
    if args.out:
        graph.save(args.out)
    print(json.dumps(summary(graph),indent=2))
