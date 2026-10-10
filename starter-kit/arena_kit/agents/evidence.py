"""Evidence from the data package for the LLM buyer: per supplier and part, the rows that move a
negotiation - price history, past asks versus settlements, scorecard, index moves behind the
cost drivers, forecast, should-cost, contract, the supplier's finances, how late we pay, and the
latest letters and notes. Every line carries the ID to cite and the figure the arena checks."""

import logging

log = logging.getLogger("arena")


def _rows(package, sql, *params):
    try:
        return package.query(sql, params)
    except Exception as e:  # one missing table must not cost the case
        log.warning("evidence query failed: %s", e)
        return []


def _index_moves(package, part):
    """Weighted cost drivers with the 12-month index change (two citable idx IDs, 12 months apart)."""
    lines, total = [], 0.0
    for d in _rows(package, "SELECT series_id, weight FROM cost_drivers WHERE part_id = ?", part):
        v = _rows(
            package,
            "SELECT id, value FROM index_values WHERE series_id = ? ORDER BY month DESC LIMIT 13",
            d["series_id"],
        )
        if len(v) == 13:
            change = (v[0]["value"] / v[12]["value"] - 1) * 100
            total += d["weight"] * change
            lines.append(f"{d['series_id']} weight {d['weight']}: {change:+.1f}% ({v[12]['id']} -> {v[0]['id']})")
    if lines:
        lines.append(f"cost-weighted move of the drivers over 12 months: {total:+.1f}% (category team's weights)")
    return lines


def dossier(package, case):
    out = []
    for part in case["parts"]:
        out.append(f"\nPART {part}")
        out += [f"  {x}" for x in _index_moves(package, part)]
        for r in _rows(
            package, "SELECT id FROM forecasts WHERE part_id = ? ORDER BY snapshot_month DESC LIMIT 1", part
        ):
            if f := package.lookup(r["id"]):
                out.append(f"  forecast {r['id']}: {f.get('change_pct')}% demand change")
        for r in _rows(
            package, "SELECT id, unit_cost, price_unit, confidence_pct FROM should_cost WHERE part_id = ?", part
        ):
            out.append(f"  should-cost {r['id']}: {r['unit_cost']} per {r['price_unit']} (+-{r['confidence_pct']}%)")
        for sid in case["suppliers"]:
            out += supplier_lines(package, sid, part)
    return "DOSSIER (data you may cite; figures are exact)" + "\n".join(out) if out else ""


def supplier_lines(package, sid, part):
    out = [f"  SUPPLIER {sid} / {part}"]
    for r in _rows(
        package,
        "SELECT valid_from, price, price_unit, event_id FROM price_conditions "
        "WHERE part_id = ? AND supplier_id = ? ORDER BY valid_from DESC LIMIT 5",
        part,
        sid,
    ):
        out.append(f"    price {r['valid_from']}: {r['price']} per {r['price_unit']} ({r['event_id']})")
    for r in _rows(
        package,
        "SELECT date, type, asked_pct, settled_pct, note FROM negotiation_history "
        "WHERE part_id = ? AND supplier_id = ? ORDER BY date DESC LIMIT 4",
        part,
        sid,
    ):
        out.append(
            f"    past {r['date']} {r['type']}: asked {r['asked_pct']}%, settled {r['settled_pct']}% {r['note'] or ''}"[
                :200
            ]
        )
    for r in _rows(
        package,
        "SELECT id, otd_pct, otd_confirmed_pct, ppm, colour FROM scorecards "
        "WHERE supplier_id = ? ORDER BY quarter DESC LIMIT 1",
        sid,
    ):
        out.append(
            f"    scorecard {r['id']}: on-time {r['otd_pct']}% (vs confirmed dates {r['otd_confirmed_pct']}%), "
            f"{r['ppm']} ppm, {r['colour']}"
        )
    for r in _rows(
        package,
        "SELECT id, payment_days, incoterm, notice_days, price_basis FROM contracts "
        "WHERE supplier_id = ? ORDER BY valid_to DESC LIMIT 1",
        sid,
    ):
        out.append(
            f"    contract {r['id']}: {r['payment_days']} days net, {r['incoterm']}, notice {r['notice_days']} days, "
            f"{r['price_basis']}"
        )
    for r in _rows(
        package,
        "SELECT owner_type, turnover_band, equity_ratio_pct, credit_index, credit_index_12m_ago "
        "FROM supplier_intel WHERE supplier_id = ?",
        sid,
    ):
        out.append(
            f"    finances: {r['owner_type']}, turnover {r['turnover_band']}, equity {r['equity_ratio_pct']}%, "
            f"credit index {r['credit_index']} (12 months ago {r['credit_index_12m_ago']})"
        )
    for r in _rows(
        package,
        "SELECT AVG(julianday(paid_date) - julianday(due_date)) AS late FROM invoices "
        "WHERE supplier_id = ? AND discount_taken = 0 AND paid_date IS NOT NULL",
        sid,
    ):
        if r["late"] is not None:
            out.append(
                f"    we pay this supplier {r['late']:+.1f} days vs due date on average (never claim we pay on time)"
            )
    for r in _rows(
        package, "SELECT id, date, kind, subject FROM documents WHERE supplier_id = ? ORDER BY date DESC LIMIT 4", sid
    ):
        try:
            text = " ".join(package.document(r["id"]).split())[:450]
        except (FileNotFoundError, ValueError):
            text = ""
        out.append(f"    {r['id']} ({r['date']} {r['kind']}, {r['subject']}): {text}")
    for r in _rows(
        package, "SELECT id, date, headline FROM news WHERE supplier_id = ? ORDER BY date DESC LIMIT 2", sid
    ):
        out.append(f"    {r['id']} ({r['date']}): {r['headline']}")
    return out
