"""ATS helpers: detect which ATS a job uses, extract keywords, score a resume.

The keyword scorer mimics what simple ATS filters (Workday, Taleo, iCIMS
"knockout" keyword matching) do: literal term overlap. It's deterministic and
free, so it runs on every job before any LLM call.
"""
from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urlparse

ATS_PATTERNS = {
    "greenhouse": ["greenhouse.io", "boards.greenhouse", "grnh.se"],
    "lever": ["jobs.lever.co", "lever.co"],
    "ashby": ["ashbyhq.com"],
    "workday": ["myworkdayjobs.com", "workday.com"],
    "smartrecruiters": ["smartrecruiters.com"],
    "icims": ["icims.com"],
    "taleo": ["taleo.net"],
    "successfactors": ["successfactors", "sapsf"],
    "workable": ["workable.com"],
    "bamboohr": ["bamboohr.com"],
    "jobvite": ["jobvite.com"],
    "linkedin": ["linkedin.com"],
    "indeed": ["indeed.com"],
    "naukri": ["naukri.com"],
}


def detect_ats(url: str | None) -> str:
    if not url:
        return "unknown"
    host = (urlparse(url).netloc + urlparse(url).path).lower()
    for ats, pats in ATS_PATTERNS.items():
        if any(p in host for p in pats):
            return ats
    return "company_site"


# Multi-word and symbol-bearing tech terms that naive tokenizers break.
KNOWN_TERMS = [
    "machine learning", "deep learning", "data engineering", "data science", "computer vision",
    "natural language processing", "distributed systems", "system design", "microservices",
    "rest api", "rest apis", "graphql", "ci/cd", "unit testing", "test automation",
    "c++", "c#", ".net", "node.js", "next.js", "vue.js", "react native", "spring boot",
    "ruby on rails", "power bi", "google cloud", "gcp", "aws", "azure", "kubernetes", "docker",
    "terraform", "ansible", "kafka", "spark", "airflow", "snowflake", "databricks", "dbt",
    "postgresql", "mysql", "mongodb", "redis", "elasticsearch", "dynamodb", "bigquery",
    "pytorch", "tensorflow", "scikit-learn", "pandas", "numpy", "llm", "llms", "rag",
    "typescript", "javascript", "python", "golang", "go", "java", "kotlin", "swift", "rust",
    "scala", "sql", "nosql", "linux", "git", "github actions", "jenkins", "agile", "scrum",
    "fastapi", "django", "flask", "react", "angular", "html", "css", "tailwind",
    "product management", "stakeholder management", "a/b testing", "etl", "observability",
    "prometheus", "grafana", "oauth", "security", "networking",
]
_SYNONYMS = {
    "golang": "go", "postgres": "postgresql", "k8s": "kubernetes", "js": "javascript",
    "ts": "typescript", "rest apis": "rest api", "llms": "llm", "amazon web services": "aws",
    "google cloud platform": "gcp", "google cloud": "gcp", "sklearn": "scikit-learn",
    "nodejs": "node.js", "node": "node.js", "reactjs": "react", "react.js": "react",
}
STOP = set("""
a an and are as at be by for from has have in is it its of on or that the to with you your we our
will this they their them us who what when where how can may must should would could about into
over under within across per via also more most other such than then these those only own same so
very just not no yes all any both each few some job role team work working company candidate
experience years year strong ability skills skill knowledge required preferred plus including
using use etc e.g i.e looking join help build new great good well high based responsibilities
requirements qualifications benefits opportunity employer equal including environment
full stack hands-on hybrid remote onsite on-site office location bangalore bengaluru india hyderabad
pune chennai mumbai delhi gurgaon noida engineering engineer engineers developer developers software
design develop development teams technical modern platform platforms solutions products product
customer customers business senior junior lead years' day days time across world best part
""".split())


def _norm(term: str) -> str:
    t = term.lower().strip()
    return _SYNONYMS.get(t, t)


def extract_keywords(text: str, top_n: int = 40, exclude: list[str] | None = None) -> list[str]:
    """Known tech terms first (exact), then frequent meaningful words (3+ mentions)."""
    low = " " + re.sub(r"\s+", " ", (text or "").lower()) + " "
    skip = {w for e in (exclude or []) for w in re.findall(r"[a-z0-9]+", e.lower())}
    found: list[str] = []
    for term in sorted(KNOWN_TERMS, key=len, reverse=True):
        pat = r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9+#])"
        if re.search(pat, low):
            n = _norm(term)
            if n not in found:
                found.append(n)
    words = re.findall(r"[a-z][a-z0-9+#.\-]{2,}", low)
    counts = Counter(w.strip(".-") for w in words if w.strip(".-") not in STOP)
    for w, c in counts.most_common():
        if len(found) >= top_n:
            break
        w = _norm(w)
        if c >= 3 and w not in found and w not in skip and not any(w in f.split() for f in found):
            found.append(w)
    return found[:top_n]


def keyword_score(resume_text: str, job_text: str, exclude: list[str] | None = None
                  ) -> tuple[float, list[str], list[str]]:
    """Returns (score 0-100, matched, missing). Known tech terms weigh 4x generic words."""
    kws = extract_keywords(job_text, exclude=exclude)
    if not kws:
        return 0.0, [], []
    res = " " + re.sub(r"\s+", " ", resume_text.lower()) + " "
    rev = {}
    for k, v in _SYNONYMS.items():
        rev.setdefault(v, set()).add(k)
    matched, missing = [], []
    total = got = 0.0
    known = {_norm(t) for t in KNOWN_TERMS}
    for k in kws:
        w = 2.0 if k in known else 0.5
        total += w
        variants = {k} | rev.get(k, set())
        hit = any(re.search(r"(?<![a-z0-9])" + re.escape(v) + r"(?![a-z0-9+#])", res) for v in variants)
        if hit:
            got += w
            matched.append(k)
        else:
            missing.append(k)
    return round(100 * got / total, 1), matched, missing
