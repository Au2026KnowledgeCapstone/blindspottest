Blindspot – Checklist Resolution
Overview

The checklist subsystem allows users to define application workflows and requirements, then evaluate them against the application's discovered AppGraph.

Instead of relying on hardcoded application journeys, Blindspot uses user-defined checklists to describe what an application should allow users to accomplish.

The system uses an LLM to interpret checklist requirements and map them to existing states and actions in the discovered graph. It then verifies whether the required checkpoints are connected in the requested order and performs static feasibility analysis using recorded action preconditions and effects.

The subsystem separates requirement interpretation, graph verification, feasibility analysis and runtime testing.

Important: A successful graph resolution does not prove that an application behaves correctly. Runtime execution and goal-condition verification are separate stages.

1. Files and Responsibilities
checklists/checklist_bank.json

Stores the predefined application test checklists.

Each checklist contains:

Field	Description
id	Unique checklist identifier.
title	Name of the test.
category	Application functionality being tested.
description	Purpose of the test.
priority	Importance of the test.
version	Checklist version.
preconditions	Conditions expected before the workflow begins.
steps	Ordered user journey requirements.
goal_conditions	Expected outcomes after completing the journey.
tags	Categories and searchable labels.

The checklist bank separates test requirements from implementation logic. New application workflows can be introduced by adding checklist entries without hardcoding those workflows into the resolver.

semantic/checklist_resolver.py

The main checklist resolution and graph verification module.

It translates natural-language requirements into structured references to discovered application states and actions.

Its responsibilities include:

Checklist resolution: Uses the configured LLM backend to interpret user requirements and map them to graph objects.
Reference validation: Ensures referenced states and actions exist in the discovered graph.
Graph path verification: Uses AppGraph.path_between() to determine whether required checkpoints are connected in the requested order.
Action checkpoint handling: Finds a route to an action's source state before appending the requested action.
Feasibility analysis: Examines action preconditions and established conditions along the discovered route.
Result classification: Distinguishes between found, not found and inconclusive graph verification results.

The module exposes the following main functions:

Function	Responsibility
graph_for_prompt()	Creates a compact representation of the discovered graph for the LLM.
_validate_model_output()	Validates the structure of the LLM response.
_validate_references()	Checks that resolved requirements reference actual graph objects.
resolve_checklist()	Interprets and resolves checklist requirements.
verify_checklist()	Verifies graph connectivity between required checkpoints.
analyze_feasibility()	Analyses declared action preconditions and established conditions.
check_flow()	Coordinates resolution, verification and feasibility analysis.

The resolver does not execute browser interactions or independently verify goal conditions. Its responsibility is graph-based resolution and static analysis.

checklists/run_checklists.py

The command-line interface and checklist execution coordinator.

Its responsibilities include:

Loading the checklist bank.
Loading the discovered application graph.
Selecting an individual checklist or running all checklists.
Passing checklist requirements to check_flow().
Collecting resolution and feasibility results.
Reporting execution and runtime verification status where supported by the existing testing pipeline.
Displaying individual results and an overall summary.

This file allows users to evaluate application workflows without manually calling resolver functions.

semantic/classifier.py

Provides the shared LLM backend abstraction.

The checklist resolver reuses LLMBackend and backend_for() from this module.

This provides consistent model configuration and provider communication without introducing a separate AI client specifically for checklist resolution.

discovery/graph.py

Provides the AppGraph data model used by the resolver.

The checklist subsystem relies on this module as the source of truth for discovered application structure.

Important graph components include:

State: Represents a discovered application state.
Action: Represents a transition between two states.
AppGraph: Stores states, actions and graph metadata.
path_between(): Finds a route between two states using breadth-first search.
is_closed(): Determines whether unexplored actions remain in the discovered graph.

The resolver does not invent application states or transitions. It uses the discovered graph to validate the LLM's interpretation.

2. Architecture and Workflow

The checklist subsystem follows this process:

A checklist is loaded from checklist_bank.json or supplied directly by the user.
run_checklists.py loads the discovered AppGraph.
The runner passes the checklist and graph to check_flow().
resolve_checklist() sends a compact graph representation and the user requirements to the configured LLM backend.
The LLM returns structured requirements, including checkpoints, preconditions and goal conditions.
The resolver validates all references against the actual graph.
verify_checklist() checks whether the required checkpoints are connected in order.
analyze_feasibility() examines the declared action requirements along the resulting route.
Results are returned to the runner for reporting.
Architecture Diagram
text
             checklist_bank.json
                     |
                     v
              run_checklists.py
                     |
                     v
            semantic/checklist_resolver.py
                     |
          +----------+----------+
          |                     |
          v                     v
    classifier.py          discovery/graph.py
     LLM backend             AppGraph
          |                     |
          v                     v
    Requirement             Graph reference
    interpretation          validation
          |                     |
          +----------+----------+
                     |
                     v
             Graph verification
                     |
                     v
             Feasibility analysis
                     |
                     v
              Checklist results
                     |
                     v
          Downstream testing pipeline

3. Checklist Result Classification

Graph verification produces three possible statuses.

Status	Meaning
found	All resolved checkpoints are connected in the required order.
not_found	A qualifying route could not be found in a closed discovered graph.
inconclusive	Requirements could not be mapped reliably, or the graph does not contain enough discovery evidence to establish a negative result.

The distinction between not_found and inconclusive is important.

An unexplored action does not prove that a journey is impossible. It means discovery may not have investigated every available route.

Feasibility Status

Feasibility is evaluated separately from graph verification.

Status	Meaning
feasible	No unsatisfied declared action requirements were identified along the discovered route.
inconclusive	The route is unavailable or the required conditions cannot be established from the available graph information.

A feasible result is a static assessment, not proof of runtime executability.

4. Example Checklist

The following is an example of a successful login checklist:

json
{
  "id": "AUTH-001",
  "title": "Successful login",
  "category": "authentication",
  "description": "A user can authenticate using valid credentials.",
  "priority": "critical",
  "version": 1,
  "preconditions": [
    "A test account exists",
    "Valid test credentials are available"
  ],
  "steps": [
    "Reach the login interface",
    "Enter valid credentials",
    "Submit the login form",
    "Reach an authenticated area"
  ],
  "goal_conditions": [
    "The user is authenticated",
    "The authenticated area is accessible"
  ],
  "tags": [
    "login",
    "authentication",
    "positive"
  ]
}


The resolver interprets these requirements and maps them to actual discovered graph objects.

For example, entering credentials does not necessarily require a separate graph transition. If the discovered graph records credentials as inputs to the Sign in action, the resolver can map the requirement to that action rather than inventing a separate field-entry transition.

5. Running the Checklist System
Prerequisites

Before running checklists:

Start the target web application.
Complete an application discovery crawl.
Ensure the discovered graph JSON file exists.
Configure the required LLM provider credentials.
Install the project's Python dependencies.
Discover the Application

Example:

bash
python -m discovery.crawler http://127.0.0.1:3000 \
    --exclude /broken \
    --credential username=demo \
    --credential password=demo123 \
    --out runs/graphs/graph_sound.json


This creates the discovered application graph used by the checklist resolver.

Run an Individual Checklist

To run the successful login checklist:

bash
python -m checklists.run_checklists \
    --graph runs/graphs/graph_sound.json \
    --checklist AUTH-001


To run the invalid login checklist:

bash
python -m checklists.run_checklists \
    --graph runs/graphs/graph_sound.json \
    --checklist AUTH-002

Run All Checklists
bash
python -m checklists.run_checklists \
    --graph runs/graphs/graph_sound.json \
    --all


This evaluates every checklist in the configured checklist bank against the supplied graph.

Makefile Commands

If the project Makefile exposes the corresponding targets, the same operations can be run through Make.

For example:

bash
make checklists


For an individual checklist, the target can accept a checklist ID and graph path, depending on the Makefile configuration.

6. Understanding the Results

An individual checklist report includes:

Output	Description
Resolution	Whether checklist requirements were successfully resolved and graph verification completed.
Graph path exists	Whether a qualifying graph path was established.
Feasibility	Static feasibility assessment of the discovered route.
Execution	Status of downstream test execution.
Runtime verification	Whether runtime behaviour has been verified.
Matched checkpoints	Requirements successfully mapped and verified.
Unresolved requirements	Requirements that could not be reliably mapped.
Goal conditions	Expected outcomes retained for subsequent verification.
Explanation	Description of the result.
Example
text
AUTH-001 - Successful login

Resolution: found
Graph path exists: True
Feasibility: feasible
Execution: not_started
Runtime verification: not_started

Matched checkpoints:
  [MATCH] Reach the login interface
  [MATCH] Enter valid credentials
  [MATCH] Submit the login form
  [MATCH] Reach an authenticated area


This indicates that the login journey was mapped to connected graph objects and passed static feasibility analysis.

It does not indicate that the application has already been tested at runtime.

7. Design Principles

The checklist subsystem follows several important principles:

User-driven requirements

Application journeys are defined through user-provided checklists rather than hardcoded workflows.

The discovered graph is the source of truth

The LLM interprets requirements but cannot invent states, transitions or identifiers.

Resolution and verification are separate

Mapping a requirement to a graph object does not automatically establish that a complete route exists.

Feasibility is separate from connectivity

A connected route may still contain actions whose declared preconditions cannot be established.

Incomplete discovery must not produce unjustified negative results

Unexplored actions are treated as missing evidence rather than proof that a journey is impossible.

Runtime behaviour requires runtime evidence

Graph verification and feasibility analysis do not replace actual browser execution or goal-condition verification.

8. Scope and Limitations

The checklist subsystem currently provides natural-language requirement resolution, graph reference validation, graph path verification and static feasibility analysis.

It does not independently:

Execute browser interactions.
Verify that application mutations persist.
Evaluate goal conditions against live browser state.
Prove that inferred transitions are executable.
Guarantee that action preconditions are satisfied by the real application.

These responsibilities belong to the downstream knowledge and runtime testing pipeline.

9. Summary

The Blindspot checklist subsystem bridges user-defined application requirements and the application's discovered graph.

It allows workflows to be described independently of application-specific implementation details, uses an LLM to interpret those requirements, validates the resulting graph references, checks route connectivity and analyses declared action requirements.

Its purpose is to provide a structured foundation for evaluating complete application journeys and passing those requirements into downstream automated testing and regression verification.