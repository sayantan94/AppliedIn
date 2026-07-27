---
name: Apple
match_hosts: [jobs.apple.com]
match_companies: [Apple]
success_phrases:
  - "thank you for applying"
  - "your application has been submitted"
  - "application received"
---

## Discovery on jobs.apple.com/en-us/search

- THE UNFILTERED SEARCH IS USELESS: location alone returns **600+ results**, and
  because the default sort is Newest, the top of that list is whatever Apple
  posted most recently, which is overwhelmingly retail and internships. Real
  first results with only `location=united-states-USA` applied: "US - Specialist:
  Seasonal, Part time", "Applied Data Solutions Program (Internships)". Any run
  that reads the first page of this and stops has found nothing useful.
- NARROW BY **Teams** FIRST, before anything else. It is the facet that removes
  the noise. The values that matter for an engineering search are
  **Software and Services** and **Machine Learning and AI**. The other teams
  (Apple Retail, Hardware, Corporate Functions, Sales and Business Development)
  are what the 600+ is mostly made of.
- THEN STACK **Keyword** FILTERS, which is the part worth understanding: the
  sidebar "Keyword" facet is NOT the big search box at the top. Type a keyword,
  press Enter, and it becomes a checked chip ("Keyword: machine learning") while
  the input clears so you can add another. They accumulate, so several of the
  owner's topics can be applied at once instead of running one search per term
  and merging by hand. The applied count above the results confirms it
  ("2 Filter(s) Applied").
- USE THE OWNER'S OWN WORDS for those keyword chips: the topics listed under
  TOPICS THAT RAISE FIT in the brief, not a generic guess. Combine them with the
  target titles rather than choosing between them, since a role can be titled
  plainly ("Software Engineer, SDLC Analytics") and still be exactly the work, or
  titled attractively and be a retail programme.
- URL SHORTCUT for a repeatable search, if driving the facets is slow:
  `https://jobs.apple.com/en-us/search?location=united-states-USA&key=<keyword>`
  The keyword value is DOUBLE encoded, so a space is `%2520` rather than `%20`
  (`key=machine%2520learning`). Getting that wrong silently returns everything.
- SET **Sort by: Newest** YOURSELF, every run. Do not assume it is already
  selected: the control sits above the results on the right and it does NOT
  default to Newest, so a run that skips this step reads the list in Apple's own
  order. That matters more than it sounds, because the crawl stops at a fixed
  number of postings: newest first means the cut discards roles earlier runs
  already saw, while any other order leaves genuinely new postings below the cut
  where they are never returned. Confirm it reads "Sort by: Newest" before you
  start collecting.
- Skip anything whose title contains Internship, Program, Seasonal, Part time, or
  "US - Specialist" — those are retail and early career, and Apple posts a great
  many of them.

## Applying

- Apple's postings live on jobs.apple.com and the application is on the same
  domain, so no board rewrite is needed. The apply link is labelled
  **"Submit Resume"**, not Apply.

### "Add related skills to your profile" — fill this properly, it is not optional

Partway down the form is a panel headed **"Add related skills to your profile."**
It is the highest value thing on the page and the easiest to skip, because
nothing marks it required and it looks like a suggestion box.

What it contains:

- A grid of SUGGESTED skills, each with a plus icon: "BM25", "Embedding
  Generation", "Hybrid Search", "RAG Pipelines", "Ranking", "SQL", "Vector
  Search". These are generated from THIS posting, so they are the vocabulary the
  role is screened on.
- A search field, "Add a new skill to your profile", for anything not suggested.
- Skills already on the profile, shown as green ticked chips with an x to remove.
- An **Add Skills** button.
- Below it, an optional proficiency section for the skills now on the profile.

How to work it:

1. Add EVERY suggested skill the owner genuinely has. The task gives you a
   SKILLS THE RÉSUMÉ EVIDENCES list; check each suggestion against it and click
   its plus when it matches. Do not stop at two or three: the panel exists to
   match the profile against the posting's own keywords, and a skill left unadded
   is a match thrown away.
2. Then add what the suggestions missed. Work through that same list and type
   anything relevant into the search field. This is where a generically titled
   profile becomes a specific one.
3. **Click Add Skills.** Selections are not saved until you do, and a panel full
   of chosen skills that was never committed looks identical to one you filled.
4. Set proficiency if the section appears, but only where the résumé supports the
   level. It is optional and a wrong claim is worse than a blank.

The one limit: a skill is a claim about the owner, so add only what the evidence
list supports. "As many as possible" means every one they can defend, not every
one on offer. If a suggested skill is not in that list, leave it.
- An Apple ID sign in may be required before the form appears. If it asks for one
  and no session exists, stop and report it rather than creating an account.
