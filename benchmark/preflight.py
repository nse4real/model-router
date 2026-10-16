from decimal import Decimal, ROUND_FLOOR


def estimate(dev_costs, heldout_count, allocation="12", safety_factor="1.25"):
    """dev_costs: measured complete A/B/C-cold/C-warm total per matched prompt.
    These must include retry, failure, evaluation, and unknown charges.
    """
    costs = [Decimal(str(c)) for c in dev_costs]
    if not costs or any(not c.is_finite() or c < 0 for c in costs):
        raise ValueError("Measured dev costs are required")
    unit = sum(costs) / len(costs) * Decimal(safety_factor)
    allocation = Decimal(allocation)
    if allocation < 0 or heldout_count < 1:
        raise ValueError("Invalid allocation or sample size")
    admitted = heldout_count if unit == 0 else min(heldout_count, int(
        (allocation / unit).to_integral_value(rounding=ROUND_FLOOR)))
    return dict(requested=heldout_count, admitted=admitted, reduction=heldout_count-admitted,
                expected_cost=str(unit * admitted), per_prompt_estimate=str(unit),
                allocation=str(allocation), safety_factor=str(safety_factor))
