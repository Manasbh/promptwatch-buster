# Playbook: from report to action

Evidence as of November 2026. Tiers: **Strong** means convergent lab and within-site evidence. **Moderate** means consistent but observational or lab-only. **Null** means tested with no detectable effect. Most AEO data comes from vendors; tiers reflect that.

## Contents
1. Diagnosis matrix: finding to action
2. What works, what doesn't (evidence tiers)
3. Refuse these (myths)
4. Technical checks
5. Platform notes
6. Compliance lines
7. Business reality for expectation-setting

---

## 1. Diagnosis matrix

| Report shows | What it usually means | Do this | Tier |
|---|---|---|---|
| Not mentioned, not cited; competitors named often | The brand isn't in the engine's consensus or memory. Brands in the model's no-search prior were written into ChatGPT's search queries 67% vs 11% of the time. | Win coverage on the **third-party domains in the outreach list**: listicles, review sites, comparison articles, communities, YouTube. Target those cited on the prompts in the Gaps list first. | Moderate |
| Cited but rarely mentioned ("ghost citation") | The engine uses the page as a source but credits facts, not the brand | Name the brand in the cited passage, title and H1. Add first-party facts only you can state: pricing, specs, limits, benchmarks. | Moderate |
| Mentioned but rarely named first | Present, but not the consensus pick | Get more independent evidence: reviews (Trustpilot/G2/Capterra; brands with no profile showed a 1% median citation rate vs 53.5%), analyst and press coverage, case studies with numbers. Fix the comparison pages engines cite. | Moderate |
| Google AI Overview absent from citations, organic top-10 low | The Google index doesn't rank you, and AI Overviews draw from it | Classic SEO first: indexability, relevance, links. For AI features Google says "no additional technical requirements". | Strong |
| Ranks organically but AI Overview doesn't cite you | A different URL from your site, or a weak passage match | Cover the sub-questions behind the query (fan-out). Lead each section with a direct answer to the sub-question. | Strong (relevance) |
| ChatGPT fan-out queries contain terms your pages lack (year, "pricing", "vs", "alternatives") | Retrieval mismatch | Rewrite titles and lead passages to match those queries. A one-SD better title match gave +14%, best-paragraph match +15.5%, within site. Build missing pages for recurring fan-out themes. | **Strong** |
| Your own pages rarely in "pages that get cited" | Engines prefer third-party pages for this intent | Normal: about 82–85% of brand-related citations are third-party. Shift effort off-site rather than producing more owned content. | Moderate |
| High "no-citation answers" share (ChatGPT/Gemini) | Answered from memory | Only the long game moves this: brand presence across the web over months. Track mention rate, not citation, for these prompts. | Moderate |
| Gemini differs sharply from Google AI Overviews | Different surfaces, same index | Check Merchant Center and Business Profile (commerce/local). Otherwise treat as noise unless the ranges separate. | Weak |
| Negative or wrong claims about the brand | The engines are reading a source that says it | Find the cited URL in the raw data (`waves/<w>/raw.jsonl`, the answer's citations), fix or answer at the source, and publish the correct fact clearly on your site. | Moderate |
| A competitor dominates one intent (e.g. all "alternatives" prompts) | They own the comparison content for that intent | Earn placement in the top-cited comparison pages for that intent. Publish an honest comparison page (no fake neutrality). | Moderate |
| **Movable prompts** (named in 20–80% of runs) | The engine already treats you as a candidate | Fastest wins: tighten the passage match on your best page for that prompt, and get onto 1–2 of the pages cited there. Re-measure these first. | Moderate |
| **Pages naming rivals but not you** (`pwb.py pages`) | The exact sources feeding rival recommendations | Ask for inclusion with real proof, offer data or a review account, or correct omissions on listicles that accept updates. Check the page is genuinely editorial before pitching; never buy undisclosed placement. | Moderate |
| **Untracked brands with high share** | A rival you didn't know about, or a category leader | Add them to `competitors` and re-run `report`. Study the sources they're named alongside (section 6/7). | — |
| Gap prompt where GSC shows you rank top 10 | Google ranks you but AI answers skip you | Highest-leverage fix: rewrite that page's title and lead passage to answer the prompt directly and name the brand in it; check the AI Overview citations for which URL wins instead | Strong (relevance) |
| Fan-out query → **no page** (section 10) | ChatGPT searches for something you have no page for | Build one page per recurring fan-out theme, or retitle the closest match shown. Watch for patterns: the year, "pricing", "for small business", "alternatives". | Strong (relevance) |
| Wrong facts in "how engines describe you" | A stale or wrong source | Trace the cited URL in `runs.csv`/raw data, get it corrected, and state the fact on your own pricing or feature page in server HTML | Moderate |

**Where the outreach list comes from:** the report's "Where the engines get their answers" section. Prioritize domains that (a) appear on Gap prompts, (b) are cited by more than one engine and (c) aren't competitors. Reddit, YouTube and Wikipedia lead most citation datasets, but platform shares swing hard. ChatGPT's Reddit citations dropped sharply twice within 12 months, so never bet the plan on one host.

## 2. Evidence tiers

| Tactic | Verdict | Key evidence |
|---|---|---|
| Query and topic match (title, best passage) | **Strong** | Decisive across 6 LLMs in 252,000 lab trials; +15.5% / +14.1% within site in a 158,099-answer audit |
| Ranking high in the engine's own retrieval | **Strong** | ChatGPT cited 43.8% of rank-1 pages vs 3.5% at ranks 11–20; retrieval explained 32% of within-site citation variance vs 2.8% for page features |
| Brand known to the model / web-wide mentions | **Moderate** | YouTube mentions ρ≈0.74 and branded web mentions ρ≈0.66–0.71 with AI visibility vs Domain Rating ρ≈0.27–0.33 (75,000 brands, correlational) |
| Specific facts: price, specs, comparisons | Moderate (lab) | Significant across models in commercial scenarios |
| Real freshness | Moderate in lab, weak in field | Real updates only; AI Overviews cite slightly older content than organic results |
| Key content early on the page | Weak–moderate | 44% of ChatGPT citations came from the first 30% of a page; no intervention test |
| Adding statistics / quotes / citations (the "GEO" tricks) | **Did not replicate** | Null or negative in C-SEO Bench (NeurIPS 2025), SAGEO Arena (KDD 2026) and GPT-5.x retests |
| FAQ blocks | Null | +18% across sites turns into −3.4% (not significant) within sites |
| JSON-LD schema | Null for citations | Matched diff-in-diff on 1,885 pages: no lift. Keep it for rich results and entity hygiene only. |
| Tables, lists, headings | Null for citation | Use them for humans |
| Author bylines | Null / inconclusive | |
| Content length | Null | Shorter pages are slightly favored at equal rank |
| llms.txt | Null | 97% of about 38,000 files got zero requests; no citation relationship across about 300,000 domains |
| Markdown served to bots | Null | Randomized, 381 pages, not significant |
| 40–60 word answer capsules | Unproven | No credible controlled test |

"Null" means no detectable average effect; small lifts on specific sites can't be ruled out. **No content tactic has a replicated causal lift on production engines.** That's why the treated-vs-holdout diff exists.

## 3. Refuse these (say why once, move on)
- "GEO adds 40%". It was a simulated GPT-3.5 engine; the Perplexity test used uploaded files. "Cite sources +40%, statistics +37%, quotes +30%" mixes methods and metrics, and Cite Sources actually scored −23% on one Perplexity metric.
- "115% boost for low-ranked sites". That held only when every source was optimized, and rank-1 sources lost 30%.
- llms.txt, special schema or chunking "for AI". Google says none are needed; elsewhere there's no evidence.
- Changing dates without real updates. It's deceptive, and Google requires dates to reflect real changes.
- Tracking a stable "AI rank". The same order shows up in under 0.1% of repeat runs.
- "Block Google-Extended to leave AI Overviews". It doesn't. Use the Search Console generative-AI control or `nosnippet`.
- "AI traffic converts 23x". That was one company. The cross-site median is 1.26x, and a peer-reviewed study of 973 e-commerce sites found organic converting better.
- "6.5x more visible from off-site". This misquotes a source-mix statistic. "84% of citations are press". Journalism was 22%. "Wikipedia is half of ChatGPT citations". It's about 8–10% overall.

## 4. Technical checks (quick pass, only the hard requirements)
- **Crawler access** (robots.txt plus the WAF/CDN, which often blocks bots silently):
  - **Search or index bots:** allow `OAI-SearchBot`, `Claude-SearchBot`, `PerplexityBot`, `Googlebot`, `bingbot`, `meta-webindexer`, `Amzn-SearchBot`, `Applebot`.
  - **Training bots:** `GPTBot`, `ClaudeBot`, `Google-Extended`, `Applebot-Extended`, `meta-externalagent`, `Amazonbot` and `CCBot` are a separate business decision. Blocking them doesn't remove you from answers.
  - **User fetchers:** `ChatGPT-User`, `Claude-User`, `Perplexity-User` and `Google-Agent` often don't promise to follow robots.txt.
  - `anthropic-ai` and `claude-web` are retired tokens. No first-party docs exist for xAI or ByteDance tokens.
- Answer-critical content (prices, specs, core answers) must be in the **server HTML**. Most AI crawlers don't execute JavaScript; Googlebot does. JSON-LD must mirror visible facts, never be their only home.
- Indexable, snippet-eligible pages; `nosnippet`/`max-snippet` limit AI Overviews too.
- **Deadlines:** UK publisher controls over Google AI features from 3 Dec 2026. Bing AI Training/Grounding preferences take effect 1 Feb 2027, and Bing stops reading NOARCHIVE/NOCACHE as AI preferences on 1 Mar 2027. `__usage_ai-training` is the emerging shared training opt-out (Anthropic now, Microsoft Feb 2027, Google early 2027).
- **Merchants:** feed accuracy (price, availability, identifiers) for Google Merchant Center/UCP and ChatGPT product discovery matters more than any page tweak.

## 5. Platform notes
- **Google AI Overviews / AI Mode:** the Google index and core ranking. AI Overviews appear on roughly 40–50% of US searches (much higher on some commercial panels, so measure your own). Top-cited sources include YouTube, Reddit and Facebook. AI Mode queries run 3x longer; cover multi-part questions in depth. Search Console reports AI impressions (not clicks) worldwide since 31 Aug 2026.
- **ChatGPT:** mixed retrieval. Google plus OpenAI's API recovered only about 27% of its cited URLs, so a Google rank doesn't predict it. Fan-out ranges from 1 to 12 queries per run depending on the model. Wikipedia, Reddit and editorial review sites are prominent. Referrals carry `utm_source=chatgpt.com`.
- **Gemini app:** the Google index; Reddit and YouTube are heavy. Its answers often ask clarifying questions on broad prompts, which shows up as low mention rates; consider more specific prompts.
- **Copilot:** Bing index. Bing Webmaster Tools reports citations and sampled grounding queries, so use it. This monitor doesn't cover Copilot directly.
- Day-to-day citation churn is 50–83% per engine, and about 75% of dropped citations return. Never act on one run.

## 6. Compliance lines (non-negotiable; protects the brand)
- Since 15 May 2026, Google's spam policies cover "attempting to manipulate generative AI responses"; demotion or removal can follow.
- **FTC Consumer Review Rule:** fake or incentivized-without-disclosure reviews and fake "independent" review sites carry up to $53,088 per violation.
- Don't use hidden instructions or prompt injection in pages, fake Reddit or forum accounts, undisclosed paid placements, fabricated stats or dates, or "summarize with AI" links that plant memory preferences (Microsoft documented 31 companies doing this). Disclose relationships. Wikipedia requires notability from independent sources plus paid-editor disclosure, so it's an outcome of coverage, not a tactic.
- Self-promotional "best X" listicles still get cited, but 69% of their citations came in answers that recommended someone else. They're weak for winning the pick, and illegal if presented as independent.

## 7. Business reality
- AI assistants send about 1% of sessions (0.2–3%); organic is often 30–45%. Losing 10% of a 40% organic share outweighs a 1% AI channel even at 3x conversion.
- AI Overviews cut organic clicks by 39.8% on queries where they appear (randomized, US desktop). Being cited roughly halves the loss (cited 2.07% vs uncited 0.94% CTR, no-Overview 3.35%; the cited premium is about 29% after removing one outlier).
- Plan on 1.0–1.3x organic conversion for AI referrals until your own data shows otherwise.
- Best ROI: high-ACV B2B, expert services, considered e-commerce. Low priority: commodity e-commerce, pre-PMF startups (just be crawlable and clearly positioned). Ad-funded publishers: defense only.
- Prove value with a 90-day pilot on matched topic clusters with holdouts. Scale only if treated beats holdout on qualified outcomes.
