"""The autonomous buyer: deterministic economics and closure, the model for reading and writing."""

from .agent import BuyerAgent


def make_buyer_factory(data=None, llm=True, store=None):
    """A zero-argument factory for `play_all`/`play_waves`: every case gets its own BuyerAgent; the
    data package, the model and the Decisions adapter (from the environment) are shared.
    `llm=False`: no model calls at all (templates and rules only), e.g. for a local eval."""
    from .decisions import decisions_from_env
    from .model import model_from_env

    package = None
    if data:
        from ..data import DataPackage

        package = DataPackage(data)
        package.lookup("")  # build the ID index once, before agents share it
    model, decisions = (model_from_env(), decisions_from_env()) if llm else (None, None)
    return lambda: BuyerAgent(package, model, decisions, store=store)


__all__ = ["BuyerAgent", "make_buyer_factory"]
