# Competition buyer

The buyer uses one case coordinator, deterministic contract economics, three online model roles and an offline critic. It retains Python, HTTPX and the existing Arena runner.

## Roles and prompts

`buyer/prompts.py` contains versioned prompts, strict output schemas, examples and fallbacks.

| Role | Primary | Effort | Output |
|---|---|---|---|
| Preparation | gpt-6.1-sol | high | Source-backed private brief: facts, estimates, hypotheses, interests, risks, levers, questions and permitted disclosures |
| Supplier interpreter | gpt-6.1-sol | high for material ambiguity | Grounded observations, conditional trades, dependencies and answered/refused questions |
| Communication selector | gpt-6.1-sol | low | Approved package, claim, question, acknowledgment and trade IDs; private rationale |
| Offline critic | gpt-6.1-sol | high | General replay findings and synthetic counterexamples |

The fallback is gpt-6-luna with low effort. `LLM_MODEL` remains the default primary; the optional
`LLM_PREPARATION_MODEL`, `LLM_INTERPRETATION_MODEL`, `LLM_COMMUNICATION_MODEL` and
`LLM_CRITIC_MODEL` variables override it per role. A practical latency split is a strong model for
preparation and interpretation, with a faster model for communication. Supported OpenAI Chat
Completions requests use Priority processing on the official endpoint. The actual returned tier is
logged, including downgrades. Priority improves speed but cannot guarantee completion inside the
Arena deadline. [Fast mode](https://developers.openai.com/api/docs/guides/fast-mode),
[reasoning](https://developers.openai.com/api/docs/guides/reasoning).

Preparation runs before opening. Direct callers supplying already-open threads receive code-only preparation. Briefs and observations are persisted with an input hash; changed case data invalidates the memo. Interpretations are cached by message identity and content. Optional OpenAI Decisions classification has separate configuration; its confidence is not a supplier acceptance probability.

## Decision flow

1. The runner captures receipt times before attachments. TurnContext carries absolute deadlines, revisions, the wave cap and in-flight suppliers.
2. The agent restores its brief and rebuilds canonical histories. Production-cost priors stay anchored to opening evidence; concessions do not change manufacturing cost estimates.
3. Relevant replies are interpreted before package generation. Material ambiguity can use high effort; ordinary replies use fixed classification or conservative rules. Dependencies and question history affect follow-up.
4. Code computes feasible packages, alternative cost, reservations, concession pace and remaining time. Reservations override aspirations and previous bids. RFQ comparisons use comparable feasible contracts.
5. The communication role selects approved IDs. Code renders every public sentence. Private rationale is never sent. Claims are checked again against the selected package.
6. Every send, acceptance and fallback passes contract and financial checks.

### Adaptive portfolios

The deterministic policy opens with up to three safe Pareto-diverse packages, then narrows later
turns to at most two. Grounded conditional trades rank matching packages first; a holding supplier
still receives at least one package that preserves estimated supplier value when one exists, while
another safe branch may remain for exploration. The live supplier price bounds selected/generated unit
prices when feasible candidates at or below it exist; if none do, selection keeps the safest cheapest
feasible branch, which may be above that live price.

The communication model selects only approved IDs. Safe omissions are completed from the approved
menus and logged as `writer=llm_normalized`; `writer=llm` means no completion was needed and
`writer=template` means the model was absent, late or invalid. Cost, must-haves, acceptance and
walk-away remain deterministic.

Supplier-specific should-cost uses the high relationship scenario; weak priors use the middle scenario. Bidding, ranking and acceptance share the same choice. All scenarios remain available for diagnosis. `cost.py` remains the single source of truth.

## Deadlines and recovery

Queueing, classification, interpretation, selection and fallback share one absolute budget. The runner reserves five seconds for dispatch; the provider chain reserves eight seconds for fallback. Insufficient time skips optional model work. Abandoned calls retain capacity until their HTTP work ends and cannot overwrite a newer decision.

Actions are recorded atomically before sending and retried under their original move IDs. Absence from a GET does not prove POST failure. A closure barrier stops proposals, settles pending actions and refreshes standing offers. Final-result polling and transient final-case failures have bounded recovery.

Concurrent sends have one pending mutation per supplier and up to three bounded supplier workers
by default. An explicit `supplier_parallel` constructor argument can reduce that number. Synthetic
tests exercise the concurrent path without Arena quota.

## Configuration and evaluation

The root README lists configuration names. Role settings apply to the buyer API adapter, independently of Claude's coding-agent settings. Credentials are never included in traces or configuration reports.

Local evaluation exercises production preparation and durable transport. Buyer and supplier model switches are independent. Reports identify source, data, model configuration, opponent, seed, completion, scoring validity, errors and latency. Repeated identical seeds are not independent evidence.

Simulator scoring filters must-haves, includes the alternative and prices relationship costs from hidden supplier economics. A world without a valid opening benchmark is explicitly unscorable. Evidence verification checks figures and scope rather than ID existence alone.

Test generalization by varying IDs, supplier order, preferences, constraints, uncertainty and alternative strength independently. Keep policy parameters fixed on held-out supplier behaviors. The scripted simulator remains limited as a persuasion benchmark.

Medium-versus-high reasoning must be compared during the next permitted model evaluation. This implementation performs no hosted practice, sandbox or real-model benchmark.
