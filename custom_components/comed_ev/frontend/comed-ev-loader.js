/**
 * Loads the card bundle and retries if the fetch fails.
 *
 * The browser never retries a failed module fetch, so one dropped request
 * (e.g. while the iOS app roams between the internal and external URL) left
 * the cards undefined until a full reload. Lovelace re-renders a card that
 * shows "Custom element doesn't exist" when its element is defined later.
 * A failed import is cached per URL, so each retry uses a new URL.
 */
const MAX_ATTEMPTS = 8; // ~2 min of backoff, then give up (e.g. a bundle bug)
const here = new URL(import.meta.url);

const load = (attempt) => {
  const url = new URL("comed-ev-cards.js", here);
  url.search = here.search;
  if (attempt) url.searchParams.set("retry", attempt);
  import(url.href).catch((err) => {
    if (attempt + 1 >= MAX_ATTEMPTS) throw err;
    console.warn("ComEd EV cards failed to load, retrying", err);
    setTimeout(() => load(attempt + 1), Math.min(30_000, 1_000 * 2 ** attempt));
  });
};
load(0);
