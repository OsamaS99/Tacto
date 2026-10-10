"""Typed handoffs between the buyer's components. Value records are frozen; the state records are
owned and mutated by one writer (the agent of that case). Private figures (C, the alternative,
reservation, estimates) live in state and evaluation records only - never in a PublicMessage."""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TurnContext:
    """Canonical receipt times from the runner, never reset by interpretation or document loads.

    decision_deadline already excludes the dispatch margin; all model work shares that budget.
    """

    session_id: str | None = None
    observed_at: float = 0.0
    due_at: dict[str, float] = field(default_factory=dict)
    revisions: dict[str, int] = field(default_factory=dict)
    wave_deadline: float | None = None
    decision_deadline: float | None = None
    closing: bool = False
    in_flight: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NegotiationBrief:
    """Private, source-backed preparation; hypotheses never become public claims."""

    facts: tuple[dict, ...] = ()
    interests: tuple[dict, ...] = ()
    risks: tuple[str, ...] = ()
    lever_priority: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()
    permitted_disclosures: tuple[str, ...] = ()
    playbook: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SupplierObservation:
    message_id: str
    fingerprint: str
    intent: str = "hold"
    firmness: int = 1
    cash_pressure: bool = False
    values: tuple[str, ...] = ()
    conditional_trades: tuple[dict, ...] = ()
    dependencies: tuple[dict, ...] = ()
    answered: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    source: str = "rules"

    def labels(self):
        return {
            "intent": self.intent,
            "firm_final": self.firmness == 3,
            "cash_pressure": self.cash_pressure,
            "values": list(self.values),
        }


@dataclass(frozen=True, slots=True)
class Claim:
    """One checkable argument: the exact sentence (ID and figure in it) the arena verifies."""

    cid: str  # stable key, e.g. "idx:PPI-D351"
    ids: tuple[str, ...]  # citable IDs the sentence uses (each counts once per case)
    sentence: str  # supplier-facing, figure pre-verified against facts.json
    kind: str  # idx | sc | qn | cost | quote | offer | po | fc | notice | news
    strength: float = 1.0  # higher first
    suppliers: tuple[str, ...] = ()  # empty: about every supplier of the case
    per_supplier: bool = False  # a rival's quote/offer counts once per supplier
    value: float | None = None  # price-type claims: the EUR figure in the case's price unit


@dataclass(slots=True)
class EvidenceDossier:
    case_id: str
    claims: list[Claim] = field(default_factory=list)  # buyer-favourable, citable
    notes: list[str] = field(default_factory=list)  # internal context for the model, never quoted
    justified_move_pct: float | None = None  # cost-weighted driver move (internal)
    ask_pct: float | None = None  # the supplier's announced increase, if any
    should_cost: dict[str, tuple[float, float]] = field(default_factory=dict)  # sid|"*" -> (EUR/unit, conf %)
    settled_pct: dict[str, list[float]] = field(default_factory=dict)  # sid -> past settlements
    cash_pressure: dict[str, bool] = field(default_factory=dict)  # sid -> finances worsening / small
    text: str = ""  # compact rendering for the model


@dataclass(frozen=True, slots=True)
class CasePlaybook:
    mode: str  # price_increase | renewal | rfq | bundling | raw_material | capacity | quality
    goal: str  # stabilise | secure_supply | quality_first | leverage | routine
    beta: float  # concession shape: < 1 holds early (Boulware), 1 linear
    k: float  # reciprocity: our C step per EUR of their C step
    min_rounds_accept: int  # never accept an open sheet before this many of our messages
    max_rounds: int  # messages we plan to use per thread
    p_walk: float  # prior that pushing past a final offer ends the thread
    mirror_final: bool  # may answer a partial final offer with our terms at its price
    relationship: bool  # C charges for squeezing the supplier (stabilise, secure_supply)
    llm_budget_s: float
    send_margin_s: float = 10.0
    rfq_keep: int = 2
    levers: tuple[str, ...] = ()  # trade priority, from the mode
    focus: str = ""  # one line on what the mode argues


@dataclass(frozen=True, slots=True)
class SupplierEstimate:
    """Our guess of a supplier's economics for `cost.relationship_cost` - scenarios, not facts."""

    full_cost: tuple[float, float, float]  # low, mid, high EUR per unit
    interest_rate: float = 0.08
    source: str = "default"


@dataclass(slots=True)
class SupplierThreadState:
    sid: str
    offer_id: str | None
    status: str
    standing: dict  # their standing terms as stated
    std: dict  # their standard terms (quote PDFs), for anything a sheet leaves out
    missing: list[str]
    rounds_used: int  # our messages so far
    moves_left: int
    their_c: list[float]  # C at the selected policy scenario, oldest first
    my_c: list[float]  # C of the best sheet in each of our messages, oldest first
    claim_results: list[dict] = field(default_factory=list)
    last_body: str = ""
    rev: int = 0  # messages in the thread: the canonical state revision
    label: dict | None = None  # Decision Selector labels for its last message (tune walk risk, never price)
    sheets: list[dict] = field(default_factory=list)  # every priced sheet of its last message, standing first
    my_sheets: list[dict] = field(default_factory=list)  # the sheets of our last message
    observation: SupplierObservation | None = None


@dataclass(slots=True)
class CaseState:
    """The canonical view of one case: rebuilt from the arena's threads on every turn, plus what
    only we know (the dossier, the playbook, the IDs cited, private targets)."""

    case: dict
    playbook: CasePlaybook
    dossier: EvidenceDossier | None
    threads: dict[str, SupplierThreadState]
    cited: set[str] = field(default_factory=set)  # IDs cited so far, once per case
    cited_to: dict[str, set[str]] = field(default_factory=dict)  # sid -> rival quotes/offers cited to it
    c_alt: float = float("inf")
    targets: dict[str, tuple[float, float]] = field(default_factory=dict)  # sid -> (C at anchor, C we never bid beyond)
    tau: float = 0.0  # 0..1, how far the case's clock and rounds have run
    brief: NegotiationBrief | None = None


@dataclass(frozen=True, slots=True)
class SupplierAssessment:
    concession: float  # their last step in our C (+ = in our favour)
    firmness: int  # 0 moving .. 3 holding (consecutive non-moves)
    regressed: bool  # their sheet got dearer for us
    walk_risk: float
    intent: str | None = None  # Decision Selector label, if any
    cash_pressure: bool = False
    values: tuple[str, ...] = ()  # levers it signals it cares about
    source: str = "rules"


@dataclass(frozen=True, slots=True)
class CandidatePackage:
    sid: str
    terms: dict  # complete: exactly the negotiable fields
    tag: str  # price | terms | integrative | mirror


@dataclass(frozen=True, slots=True)
class EvaluatedPackage:
    pkg: CandidatePackage
    c: float  # our C at the selected policy scenario
    c_high: float  # our C, pessimistic scenario
    cash_theirs: float  # what it is worth to the supplier per unit (cost.supplier_value)

    @property
    def terms(self):
        return self.pkg.terms


@dataclass(frozen=True, slots=True)
class DecisionProposal:
    kind: str  # send | accept | walk
    sid: str
    packages: tuple[EvaluatedPackage, ...] = ()
    claims: tuple[Claim, ...] = ()
    question: str | None = None
    term_sheet_id: str | None = None
    reason: str = ""
    source: str = "policy"  # policy | llm | fallback


@dataclass(frozen=True, slots=True)
class DisclosurePacket:
    """Everything the writer may say - and nothing private."""

    sid: str
    mode: str
    goal: str
    rev: int
    packages: tuple[dict, ...]  # the term sheets, as sent
    claims: tuple[Claim, ...]
    question: str | None
    their_last: str
    allowed_numbers: frozenset[str]
    subject: str
    status: str = "open"  # the supplier's offer status: open | final_offer | agreed
    questions: tuple[str, ...] = ()
    acknowledgments: tuple[str, ...] = ("neutral",)
    trades: tuple[str, ...] = ()
    first_contact: bool = False
    standing_price: float | None = None


@dataclass(frozen=True, slots=True)
class PublicMessage:
    subject: str
    body: str
    term_sheets: tuple[dict, ...] = ()
    refs: tuple[str, ...] = ()

    def payload(self):
        return {
            "subject": self.subject,
            "body": self.body,
            "term_sheets": [{"terms": t} for t in self.term_sheets],
            "refs": list(self.refs),
        }


@dataclass(slots=True)
class OutboxMove:
    move_id: str
    session_id: str
    sid: str
    payload: dict
    sha: str
    created_at: float
    state: str = "pending"  # pending | done | failed
    error: str | None = None
