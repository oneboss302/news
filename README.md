# news.castafero.com

Top world, U.S., business, tech and science headlines, rebuilt every 2 hours by GitHub Actions.

- `build.py` pulls the publishers' RSS feeds (standard library only), groups headlines that several outlets are covering into **Top stories**, and writes `index.html`.
- `.github/workflows/update.yml` runs it on a schedule, commits the snapshot, and deploys to GitHub Pages.
- To add or drop a source, edit `FEEDS` at the top of `build.py`.
- Manual refresh: Actions → Update news → Run workflow.

Headlines and summaries come from publisher RSS feeds; every story links to the original article.
