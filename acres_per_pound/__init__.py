"""acres-per-pound: find UK property by land value.

Scrapes Rightmove UK-wide, parses stated land size from listing text,
and ranks every listing by pounds-per-acre. Publishes a static site
(vanilla JS) that GitHub Actions updates on a cron and GitHub Pages
serves for free.
"""

__version__ = "0.1.0"
