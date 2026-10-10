"""EvidenceDossier: what the data says about this case, before the session opens."""

from ..agents.evidence import dossier as render
from .records import EvidenceDossier


def build_dossier(package, case):
    return EvidenceDossier(case_id=case["case_id"], text=render(package, case))
