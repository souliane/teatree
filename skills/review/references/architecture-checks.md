# Architecture checks — concept ownership

## The One-Place Test — Count the Files (Non-Negotiable)

The two checks above ask what is *inside* a touched file and *where* the changed files live. This one asks **who owns the concept**, and the reviewer asks it out loud of every concept the diff touches:

> If this concept changes shape tomorrow, how many files do I touch?

The answer must be **ONE**. Any higher count is a finding — state the count and name the files. "No code duplication introduced" is the weaker test, and three shapes pass it while failing this one:

1. **A module-level helper called from N call sites.** It removes the duplicated *lines* and leaves N places to edit, so the count is unchanged. The fix is an owner the call sites hand the concept to — a serializer, a renderer, a component — not a function they each re-invoke.
2. **"These two are different, so leave that one out."** Interrogate the difference before accepting it. A difference of **presentation** — a value rendered as a locale string, a date stringified for an encoder, an extra flag appended — is what a serializer field or a composed renderer is for, and exempts nothing. Only a difference of **behaviour or data shape** does.
3. **Deduplicating 3 of 5 sites.** Four remaining edits is still not one.

**Centralising is not flattening.** The shared owner holds the *invariant* and may keep deliberately-distinct *strategies* distinct — it takes the block, it never picks the strategy. In the change that produced this rule one resolver returned `None` rather than falling back to a live reading **on purpose**, its reason recorded in the code ("showing today's number beside a rate computed from an unknown one is the same defect with less excuse"), so unifying the resolvers would have reintroduced a defect that had been deliberately removed; the shared component owned the invariant — an absent key never reaches the payload as a null, an unpriced payload stays byte-identical — and took the block without picking a strategy. Over-unification is this finding pointed the other way: flag N owners for one concept, and flag one owner that erased a deliberate difference.
