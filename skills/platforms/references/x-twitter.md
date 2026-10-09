# X / Twitter Platform Reference

> Recipes for reading X (Twitter) posts. Skills reference this file via `See platforms/x-twitter.md § <section>`.

---

## Access Method

X has no read API on the free tier and gates direct fetches behind JS + auth. To read a post's content, route the URL through a mirror that returns plain JSON.

## Reading a Post

Rewrite the post URL to the mirror host and fetch it — no auth needed:

```text
https://x.com/<user>/status/<id>  →  https://api.fxtwitter.com/<user>/status/<id>
```

The JSON response carries the full text, author, date, attached media, and the resolved target of any `t.co` shortlink. For a link-tweet (the post is just a headline + link), follow the embedded article URL next and fetch that.

## Media

The JSON also returns media objects with direct URLs:

- **Photos / article-card covers** — direct `pbs.twimg.com` image URLs. Download the URL, then validate with `file` before reading it as an image (raster only).
- **Videos** — direct `mp4` variants.

## Why Not Fetch x.com Directly

A direct `x.com` / `twitter.com` fetch returns **402**. User-agent spoofing does not help — the gating is JS + auth, not UA-based — so don't bother retrying with a fake UA.

## Alternate Mirror

`api.vxtwitter.com`, same path shape:

```text
https://api.vxtwitter.com/<user>/status/<id>
```

## Last Resort

If both mirrors fail, use browser automation through the user's authenticated session (main-agent only).
