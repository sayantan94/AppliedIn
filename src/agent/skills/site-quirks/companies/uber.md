---
name: Uber
match_companies: [uber]
allow_domains: [oraclecloud.com, taleo.net, login.uber.com]
---

- Uber's "Apply" bounces from the careers page out to an **Oracle Cloud** portal
  on a different domain. That redirect is expected — follow it; the application
  cannot be completed on the careers page itself.
- The Oracle portal requires an account. Without a saved session, stop and report
  that a login is needed rather than attempting to register.
