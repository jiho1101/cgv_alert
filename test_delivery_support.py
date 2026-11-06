def acknowledge_embeds(embeds, on_delivered=None, **kwargs):
    """Source-only tests must emulate each HTTP receipt, not a whole-run boolean."""
    if on_delivered:
        for embed in embeds:
            on_delivered(embed)
    return True
