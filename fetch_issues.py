"""
Tableau Known Issues Fetcher
----------------------------
1. Launches a headless browser to capture the Coveo search token from Salesforce Help.
2. Queries the Coveo API for all Tableau known issues with status:
   In Review | Solution in Progress | Solution Scheduled
3. Paginates through all results.
4. Generates a self-contained local HTML report.

Usage:
    python3 fetch_issues.py [--statuses "In Review,Solution in Progress,Solution Scheduled"]
                            [--products "Tableau Server,Tableau Cloud,Tableau Desktop,..."]
                            [--output known_issues.html]
"""
import asyncio
import json
import sys
import argparse
import re
from datetime import datetime, timezone
from playwright.async_api import async_playwright

# ── configuration ──────────────────────────────────────────────────────────────

SALESFORCE_ISSUES_URL = (
    "https://help.salesforce.com/s/issues?language=en_US"
    "#f[sfcategoryfull]=Tableau%7CTableau%20Server"
)
COVEO_SEARCH_URL = "https://platform.cloud.coveo.com/rest/search/v2"
COVEO_ORG_ID = "org62salesforce"
PAGE_SIZE = 100  # Coveo max per request

TABLEAU_CATEGORIES = [
    "Tableau|Tableau APIs and Extensions",
    "Tableau|Tableau Bridge",
    "Tableau|Tableau Cloud",
    "Tableau|Tableau Desktop",
    "Tableau|Tableau Mobile",
    "Tableau|Tableau Next",
    "Tableau|Tableau Prep Builder",
    "Tableau|Tableau Server",
]

DEFAULT_STATUSES = ["In Review", "Solution in Progress", "Solution Scheduled"]

FIELDS_TO_INCLUDE = [
    "author", "language", "urihash", "objecttype", "collection", "source",
    "permanentid", "sfid", "sfstatus__c", "sffound_in_version_external__c",
    "sfreporting_user_count__c", "sfcreateddate", "sfcategory__rname",
    "sflast_modified_date_external__c", "sfcategory__rcloud__c",
    "sfsubject__c", "sfsummary__c", "sfslug__c",
    "sffixed_in_version_external__c",
]


# ── token acquisition ──────────────────────────────────────────────────────────

async def get_coveo_token() -> tuple[str, str]:
    """Load the Salesforce Help page in a headless browser and intercept the Coveo token."""
    token = None
    post_data_sample = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        def on_request(request):
            nonlocal token, post_data_sample
            url = request.url
            if (
                "platform.cloud.coveo.com/rest/search/v2?" in url
                and "querySuggest" not in url
                and token is None
            ):
                m = re.search(r"access_token=([A-Za-z0-9._-]+)", url)
                if m:
                    token = m.group(1)
                try:
                    post_data_sample = request.post_data
                except Exception:
                    pass

        page.on("request", on_request)
        await page.goto(SALESFORCE_ISSUES_URL, wait_until="domcontentloaded", timeout=30000)
        # Give JS time to fire the initial search
        for _ in range(20):
            if token:
                break
            await page.wait_for_timeout(500)

        await browser.close()

    if not token:
        raise RuntimeError("Could not capture Coveo access token from page. Try again.")

    return token, post_data_sample or ""


# ── coveo search ───────────────────────────────────────────────────────────────

def build_search_payload(
    categories: list[str],
    statuses: list[str],
    first_result: int = 0,
    number_of_results: int = PAGE_SIZE,
) -> dict:
    """Build Coveo search payload with Tableau facets and status filters."""
    # Build AQ (additional query) to filter by status
    if statuses:
        status_filter = " OR ".join(f'@sfstatus__c=="{s}"' for s in statuses)
        aq = f"({status_filter})"
    else:
        aq = ""

    # Build facet values for categories
    facet_values = [{"value": cat, "state": "selected"} for cat in categories]

    return {
        "locale": "en-US",
        "debug": False,
        "tab": "default",
        "referrer": "default",
        "timezone": "UTC",
        "fieldsToInclude": FIELDS_TO_INCLUDE,
        "pipeline": "Known Issues",
        "q": "",
        "aq": aq,
        "enableQuerySyntax": True,
        "searchHub": "Known_Issues",
        "sortCriteria": "date descending",
        "firstResult": first_result,
        "numberOfResults": number_of_results,
        "facets": [
            {
                "delimitingCharacter": "|",
                "filterFacetCount": True,
                "injectionDepth": 50000,
                "numberOfValues": 1000,
                "sortCriteria": "automatic",
                "type": "specific",
                "currentValues": facet_values,
                "freezeCurrentValues": False,
                "isFieldExpanded": False,
                "preventAutoSelect": True,
                "facetSearch": {"captions": {}, "numberOfValues": 10, "query": ""},
                "field": "sfcategoryfull",
                "facetId": "sfcategoryfull",
            }
        ],
    }


async def fetch_all_issues(
    token: str,
    categories: list[str],
    statuses: list[str],
) -> list[dict]:
    """Paginate through all Coveo results and return flat list of raw fields."""
    import urllib.request

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0",
    }
    url = f"{COVEO_SEARCH_URL}?organizationId={COVEO_ORG_ID}"

    all_results = []
    first_result = 0
    total_count = None

    while True:
        payload = build_search_payload(categories, statuses, first_result)
        data = json.dumps(payload).encode()

        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())

        if total_count is None:
            total_count = body.get("totalCount", 0)
            print(f"  Total matching issues: {total_count}")

        results = body.get("results", [])
        if not results:
            break

        for r in results:
            raw = r.get("raw", {})
            all_results.append({
                "id": raw.get("sfid", ""),
                "title": r.get("title") or raw.get("sfsubject__c", ""),
                "status": raw.get("sfstatus__c", ""),
                "product": raw.get("sfcategory__rname", ""),
                "cloud": raw.get("sfcategory__rcloud__c", ""),
                "found_in_release": raw.get("sffound_in_version_external__c", ""),
                "fixed_in_release": raw.get("sffixed_in_version_external__c", ""),
                "summary": raw.get("sfsummary__c", ""),
                "reports": int(raw.get("sfreporting_user_count__c") or 0),
                "created": raw.get("sfcreateddate", 0),
                "updated": raw.get("sflast_modified_date_external__c", 0),
                "slug": raw.get("sfslug__c", ""),
                "url": (
                    f"https://help.salesforce.com/s/issue?id={raw.get('sfid', '')}"
                    f"&title={raw.get('sfslug__c', '')}"
                ),
            })

        first_result += len(results)
        print(f"  Fetched {first_result}/{total_count} ...")
        if first_result >= total_count:
            break

    return all_results


# ── HTML report ────────────────────────────────────────────────────────────────

STATUS_COLORS = {
    "In Review": "#e8a838",
    "Solution in Progress": "#1589ee",
    "Solution Scheduled": "#2e844a",
    "Closed": "#747474",
    "Known": "#c23934",
}


def ts_to_date(ts_ms: int) -> str:
    if not ts_ms:
        return ""
    try:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return ""


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def generate_html(issues: list[dict], statuses: list[str], categories: list[str], generated_at: str) -> str:
    all_products = sorted({i["product"] for i in issues if i["product"]})
    all_statuses = sorted({i["status"] for i in issues if i["status"]})
    all_releases = sorted({i["found_in_release"] for i in issues if i["found_in_release"]}, reverse=True)

    rows_html = []
    for iss in issues:
        status_color = STATUS_COLORS.get(iss["status"], "#747474")
        summary_plain = strip_html(iss["summary"])[:280]
        created_date = ts_to_date(iss["created"])
        updated_date = ts_to_date(iss["updated"])
        found = iss["found_in_release"] or "—"
        fixed = iss["fixed_in_release"] or "—"

        rows_html.append(f"""
        <tr
          data-product="{iss['product']}"
          data-status="{iss['status']}"
          data-release="{iss['found_in_release']}"
        >
          <td class="title-cell">
            <a href="{iss['url']}" target="_blank" rel="noopener">{iss['title']}</a>
            <div class="summary">{summary_plain}</div>
          </td>
          <td><span class="badge" style="background:{status_color}">{iss['status']}</span></td>
          <td>{iss['product']}</td>
          <td class="release">{found}</td>
          <td class="release">{fixed}</td>
          <td class="num">{iss['reports']}</td>
          <td class="date">{created_date}</td>
          <td class="date">{updated_date}</td>
        </tr>""")

    product_options = "\n".join(f'<option value="{p}">{p}</option>' for p in all_products)
    status_options = "\n".join(f'<option value="{s}">{s}</option>' for s in all_statuses)
    rows_joined = "\n".join(rows_html)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tableau Known Issues</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f4f6f9; color: #1a1a1a; font-size: 14px; }}
  header {{ background: #032d60; color: #fff; padding: 18px 24px; }}
  header h1 {{ font-size: 20px; font-weight: 700; }}
  header p {{ font-size: 12px; opacity: 0.75; margin-top: 4px; }}
  .controls {{ background: #fff; border-bottom: 1px solid #dde; padding: 12px 24px;
               display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }}
  .controls label {{ font-size: 12px; font-weight: 600; color: #555; }}
  .controls select, .controls input {{ padding: 5px 8px; border: 1px solid #ccc;
    border-radius: 4px; font-size: 13px; background: #fff; }}
  .controls select {{ min-width: 180px; }}
  .controls input {{ min-width: 250px; }}
  .stat {{ font-size: 13px; color: #555; margin-left: auto; }}
  .stat strong {{ color: #032d60; }}
  .table-wrap {{ overflow-x: auto; padding: 0 24px 24px; margin-top: 16px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff;
           border-radius: 6px; overflow: hidden;
           box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
  thead tr {{ background: #032d60; color: #fff; }}
  th {{ padding: 10px 12px; text-align: left; font-size: 13px; font-weight: 600;
        white-space: nowrap; cursor: pointer; user-select: none; }}
  th:hover {{ background: #1a4b8c; }}
  th.sorted-asc::after {{ content: " ▲"; font-size: 10px; }}
  th.sorted-desc::after {{ content: " ▼"; font-size: 10px; }}
  tbody tr {{ border-bottom: 1px solid #f0f0f0; transition: background .1s; }}
  tbody tr:hover {{ background: #f0f7ff; }}
  tbody tr.hidden {{ display: none; }}
  td {{ padding: 10px 12px; vertical-align: top; }}
  .title-cell a {{ color: #032d60; font-weight: 500; text-decoration: none; }}
  .title-cell a:hover {{ text-decoration: underline; }}
  .summary {{ color: #666; font-size: 12px; margin-top: 3px; line-height: 1.4; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px;
            color: #fff; font-size: 11px; font-weight: 600; white-space: nowrap; }}
  .release {{ font-size: 12px; color: #333; }}
  .num {{ text-align: right; color: #555; }}
  .date {{ font-size: 12px; color: #777; white-space: nowrap; }}
  .no-results {{ padding: 40px; text-align: center; color: #888; }}
  @media (max-width: 768px) {{
    .controls {{ flex-direction: column; align-items: flex-start; }}
    .stat {{ margin-left: 0; }}
  }}
</style>
</head>
<body>

<header>
  <h1>Tableau Known Issues</h1>
  <p>Source: help.salesforce.com/s/issues &nbsp;|&nbsp; Generated: {generated_at}</p>
</header>

<div class="controls">
  <div>
    <label for="f-product">Product</label><br>
    <select id="f-product">
      <option value="">All Products</option>
      {product_options}
    </select>
  </div>
  <div>
    <label for="f-status">Status</label><br>
    <select id="f-status">
      <option value="">All Statuses</option>
      {status_options}
    </select>
  </div>
  <div>
    <label for="f-release">Found In Release</label><br>
    <input type="text" id="f-release" placeholder="e.g. 2025.1, 2024.2…" style="min-width:180px">
  </div>
  <div>
    <label for="f-search">Search title/summary</label><br>
    <input type="text" id="f-search" placeholder="e.g. upgrade, extract, login…">
  </div>
  <div class="stat">
    Showing <strong id="visible-count">{len(issues)}</strong> of <strong>{len(issues)}</strong> issues
  </div>
</div>

<div class="table-wrap">
  <table id="issues-table">
    <thead>
      <tr>
        <th data-col="0">Title / Description</th>
        <th data-col="1">Status</th>
        <th data-col="2">Product</th>
        <th data-col="3">Found In Release</th>
        <th data-col="4">Fixed In Release</th>
        <th data-col="5" title="User reports">Reports</th>
        <th data-col="6">Created</th>
        <th data-col="7">Updated</th>
      </tr>
    </thead>
    <tbody id="issues-body">
{rows_joined}
    </tbody>
  </table>
  <div class="no-results" id="no-results" style="display:none">No issues match the current filters.</div>
</div>

<script>
(function() {{
  var tbody = document.getElementById('issues-body');
  var rows = Array.from(tbody.querySelectorAll('tr'));
  var totalCount = rows.length;
  var visibleCount = document.getElementById('visible-count');
  var noResults = document.getElementById('no-results');

  var filters = {{ product: '', status: '', release: '', search: '' }};
  var sortState = {{ col: 6, dir: 'desc' }};

  function applyFilters() {{
    var q = filters.search.toLowerCase();
    var rel = filters.release.toLowerCase();
    var shown = 0;
    rows.forEach(function(row) {{
      var prod = row.dataset.product || '';
      var stat = row.dataset.status || '';
      var rowRel = (row.dataset.release || '').toLowerCase();
      var text = row.textContent.toLowerCase();

      var ok = (!filters.product  || prod === filters.product)
            && (!filters.status   || stat === filters.status)
            && (!rel              || rowRel.indexOf(rel) >= 0)
            && (!q                || text.indexOf(q) >= 0);

      row.classList.toggle('hidden', !ok);
      if (ok) shown++;
    }});
    visibleCount.textContent = shown;
    noResults.style.display = shown === 0 ? '' : 'none';
  }}

  document.getElementById('f-product').addEventListener('change', function() {{
    filters.product = this.value; applyFilters();
  }});
  document.getElementById('f-status').addEventListener('change', function() {{
    filters.status = this.value; applyFilters();
  }});
  var releaseTimer;
  document.getElementById('f-release').addEventListener('input', function() {{
    clearTimeout(releaseTimer);
    var val = this.value;
    releaseTimer = setTimeout(function() {{ filters.release = val; applyFilters(); }}, 200);
  }});
  var searchTimer;
  document.getElementById('f-search').addEventListener('input', function() {{
    clearTimeout(searchTimer);
    var val = this.value;
    searchTimer = setTimeout(function() {{ filters.search = val; applyFilters(); }}, 200);
  }});

  // Sort
  document.querySelectorAll('th[data-col]').forEach(function(th) {{
    th.addEventListener('click', function() {{
      var col = +this.dataset.col;
      if (sortState.col === col) {{
        sortState.dir = sortState.dir === 'asc' ? 'desc' : 'asc';
      }} else {{
        sortState.col = col;
        sortState.dir = 'asc';
      }}
      document.querySelectorAll('th').forEach(function(h) {{
        h.classList.remove('sorted-asc', 'sorted-desc');
      }});
      this.classList.add('sorted-' + sortState.dir);
      sortRows();
    }});
  }});

  function cellText(row, col) {{
    var cells = row.querySelectorAll('td');
    if (!cells[col]) return '';
    return cells[col].textContent.trim();
  }}

  function sortRows() {{
    var col = sortState.col;
    var dir = sortState.dir === 'asc' ? 1 : -1;
    rows.sort(function(a, b) {{
      var av = cellText(a, col);
      var bv = cellText(b, col);
      // numeric columns
      if (col === 5) {{
        return dir * ((+av || 0) - (+bv || 0));
      }}
      return dir * av.localeCompare(bv);
    }});
    rows.forEach(function(r) {{ tbody.appendChild(r); }});
  }}
}})();
</script>
</body>
</html>
"""


# ── main ───────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Fetch Tableau Known Issues into a local HTML report")
    parser.add_argument(
        "--statuses",
        default="In Review,Solution in Progress,Solution Scheduled",
        help="Comma-separated list of statuses to include",
    )
    parser.add_argument(
        "--products",
        default=",".join(TABLEAU_CATEGORIES),
        help="Comma-separated list of category paths (e.g. 'Tableau|Tableau Server')",
    )
    parser.add_argument(
        "--output",
        default="known_issues.html",
        help="Output HTML file path",
    )
    return parser.parse_args()


async def run(args):
    statuses = [s.strip() for s in args.statuses.split(",") if s.strip()]
    categories = [c.strip() for c in args.products.split(",") if c.strip()]
    output = args.output

    print("Step 1: Capturing Coveo search token from Salesforce Help page...")
    token, _ = await get_coveo_token()
    print(f"  Token captured (first 30 chars): {token[:30]}...")

    print(f"\nStep 2: Fetching issues (statuses={statuses}, {len(categories)} products)...")
    issues = await fetch_all_issues(token, categories, statuses)
    print(f"  Done. Total issues fetched: {len(issues)}")

    print(f"\nStep 3: Generating HTML report → {output}")
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = generate_html(issues, statuses, categories, generated_at)

    with open(output, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n  Report saved: {output}")
    print(f"  Open with:  open \"{output}\"")


def main():
    args = parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
