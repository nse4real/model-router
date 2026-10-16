TIERS = ("simple", "medium", "complex")


def ordered_models(config, tier, raised=False):
    if raised:
        tier = TIERS[min(2, TIERS.index(tier) + 1)]
    # Each configured chain must explicitly include lower-budget alternatives.
    return list(dict.fromkeys(config.chains[tier]))[:3]
