"""Project-level overview: stack, structure, entry points, central files."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List

from .store import Store

KNOWN = {
    # python
    "flask": "Flask", "django": "Django", "fastapi": "FastAPI", "sqlalchemy": "SQLAlchemy",
    "flask-sqlalchemy": "SQLAlchemy", "flask-login": "Flask-Login", "pydantic": "Pydantic",
    "celery": "Celery", "pytest": "pytest", "torch": "PyTorch", "tensorflow": "TensorFlow",
    "scikit-learn": "scikit-learn", "pandas": "pandas", "numpy": "NumPy", "streamlit": "Streamlit",
    "jinja2": "Jinja2", "requests": "requests", "djangorestframework": "Django REST framework",
    # js
    "react": "React", "next": "Next.js", "vue": "Vue", "nuxt": "Nuxt", "svelte": "Svelte",
    "@angular/core": "Angular", "express": "Express", "fastify": "Fastify", "koa": "Koa",
    "@nestjs/core": "NestJS", "axios": "axios", "redux": "Redux", "@reduxjs/toolkit": "Redux",
    "react-router-dom": "React Router", "tailwindcss": "Tailwind CSS", "mongoose": "Mongoose",
    "prisma": "Prisma", "@prisma/client": "Prisma", "sequelize": "Sequelize", "jest": "Jest",
    "vite": "Vite", "typescript": "TypeScript", "socket.io": "Socket.IO", "electron": "Electron",
    "jsonwebtoken": "JWT", "passport": "Passport", "firebase": "Firebase",
    # others
    "spring-boot": "Spring Boot", "gin-gonic/gin": "Gin", "gorilla/mux": "Gorilla mux",
    "actix-web": "Actix", "rails": "Rails", "laravel/framework": "Laravel",
}

FRAMEWORKS = {"Flask", "Django", "FastAPI", "React", "Next.js", "Vue", "Nuxt", "Svelte", "Angular", "Express",
              "Fastify", "Koa", "NestJS", "Spring Boot", "Gin", "Actix", "Rails", "Laravel", "Streamlit", "Electron"}

ENTRY_NAMES = {"main.py", "app.py", "manage.py", "wsgi.py", "asgi.py", "run.py", "server.py",
               "main.go", "main.rs", "index.js", "server.js", "app.js", "main.js", "index.ts",
               "server.ts", "main.ts", "app.ts", "App.jsx", "App.tsx", "App.vue", "main.jsx",
               "main.tsx", "Main.java", "Application.java", "Program.cs", "main.c", "main.cpp",
               "index.php", "config.ru"}


def _deps(root: Path) -> List[str]:
    names: List[str] = []
    for req in list(root.glob("requirements*.txt")) + list(root.glob("*/requirements*.txt")):
        for line in req.read_text(errors="ignore").splitlines():
            m = re.match(r"\s*([A-Za-z0-9_.\-]+)", line)
            if m and not line.strip().startswith("#"):
                names.append(m.group(1).lower())
    for pp in [root / "pyproject.toml"]:
        if pp.exists():
            names += [m.lower() for m in re.findall(r"^\s*\"?([A-Za-z0-9_.\-]+)\s*(?:[<>=~!\[\"]|$)", pp.read_text(errors="ignore"), re.M)]
    for pj in [root / "package.json"] + list(root.glob("*/package.json")):
        try:
            data = json.loads(pj.read_text(errors="ignore"))
            for k in ("dependencies", "devDependencies"):
                names += list((data.get(k) or {}).keys())
        except (OSError, ValueError):
            pass
    gm = root / "go.mod"
    if gm.exists():
        names += [m.split("/", 1)[-1] for m in re.findall(r"github\.com/([\w\-]+/[\w\-]+)", gm.read_text(errors="ignore"))]
    for f in ("pom.xml", "build.gradle", "build.gradle.kts"):
        p = root / f
        if p.exists() and "spring-boot" in p.read_text(errors="ignore"):
            names.append("spring-boot")
    cargo = root / "Cargo.toml"
    if cargo.exists():
        names += re.findall(r"^([\w\-]+)\s*=", cargo.read_text(errors="ignore"), re.M)
    gem = root / "Gemfile"
    if gem.exists():
        names += re.findall(r"gem\s+['\"]([\w\-]+)['\"]", gem.read_text(errors="ignore"))
    return names


def build_overview(store: Store, root: Path) -> Dict:
    files = store.all_files()
    langs = Counter(f["lang"] for f in files if f["lang"] not in ("json", "yaml", "markdown", "text", "toml", "ini", "xml"))
    dirs = Counter(f["path"].split("/")[0] + "/" for f in files if "/" in f["path"])
    stack = []
    for d in _deps(root):
        name = KNOWN.get(d.lower())
        if name and name not in stack:
            stack.append(name)
    stack.sort(key=lambda n: n not in FRAMEWORKS)  # frameworks first: "Flask, React, ..." 
    entries = [f["path"] for f in files if f["path"].split("/")[-1] in ENTRY_NAMES and f["path"].count("/") <= 2][:8]
    indeg = store.in_degree()
    central = sorted(((indeg.get(f["id"], 0), f["path"]) for f in files), reverse=True)
    central = [p for n, p in central if n >= 2][:6]
    readme = ""
    for f in files:
        if f["path"].lower() in ("readme.md", "readme.rst", "readme.txt", "readme"):
            readme = (f["doc"] or "")[:300]
            break
    http = store.db.execute("SELECT COUNT(*) AS n FROM edges WHERE kind='http'").fetchone()["n"]
    return {
        "name": root.name,
        "files": len(files),
        "languages": [f"{l} ({n})" for l, n in langs.most_common(6)],
        "stack": stack[:12],
        "top_dirs": [f"{d} ({n} files)" for d, n in dirs.most_common(8)],
        "entry_points": entries,
        "central_files": central,
        "readme": readme,
        "http_links": http,
    }


def overview_text(ov: Dict, description: str = "") -> str:
    lines = [f"Project: {ov['name']} ({ov['files']} source files)"]
    if description:
        lines.append(description)
    elif ov.get("readme"):
        lines.append("README: " + ov["readme"])
    if ov.get("stack"):
        lines.append("Stack: " + ", ".join(ov["stack"]))
    if ov.get("languages"):
        lines.append("Languages: " + ", ".join(ov["languages"]))
    if ov.get("top_dirs"):
        lines.append("Main folders: " + ", ".join(ov["top_dirs"]))
    if ov.get("entry_points"):
        lines.append("Entry points: " + ", ".join(ov["entry_points"]))
    if ov.get("central_files"):
        lines.append("Most-imported files: " + ", ".join(ov["central_files"]))
    return "\n".join(lines)
