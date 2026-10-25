"""Best-effort project labels from bounded filenames and package metadata only."""
import json
from pathlib import Path
import re
import tomllib

TYPES = ["Website", "Web app", "API / service", "Native app", "Command-line tool",
         "Library / package", "Qlik extension", "Data / notebooks", "Scripts / automation", "Unclassified"]
SKIP = {"node_modules", ".git", ".venv", "venv", ".herdr-wt", "build", "dist", "target", ".build",
        ".next", ".nuxt", ".cache", ".worker", ".orchestration", ".scratch", "artifacts", "vendor",
        "__pycache__", "coverage", ".turbo", ".igloo"}
EXTENSIONS = {".py":"Python", ".swift":"Swift", ".rs":"Rust", ".go":"Go", ".ts":"TypeScript",
              ".tsx":"TypeScript", ".js":"JavaScript", ".jsx":"JavaScript", ".mjs":"JavaScript",
              ".cjs":"JavaScript", ".java":"Java", ".kt":"Kotlin", ".cs":"C#", ".cpp":"C++",
              ".cc":"C++", ".c":"C", ".sh":"Shell", ".zsh":"Shell", ".ipynb":"Python",
              ".dart":"Dart", ".php":"PHP", ".rb":"Ruby", ".html":"HTML", ".css":"CSS"}

def metadata(path, toml=False):
    try:
        if path.is_symlink() or path.stat().st_size > 262144:
            return {}
        value = tomllib.loads(path.read_text()) if toml else json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}

def detect(root):
    root = Path(root)
    names, suffixes, paths = set(), set(), set()
    def walk(base, depth):
        try:
            entries = sorted(base.iterdir(), key=lambda p:p.name)[:500]
        except OSError:
            return
        for p in entries:
            if p.is_symlink():
                continue
            names.add(p.name.lower())
            if p.is_file():
                suffixes.add(p.suffix.lower())
                paths.add(p.relative_to(root).as_posix().lower())
            elif depth and p.name not in SKIP and not p.name.startswith(".") and not p.name.endswith((".app",".xcodeproj",".xcworkspace")):
                walk(p,depth-1)
    walk(root,2)
    package=metadata(root/"package.json")
    deps=set()
    for key in ("dependencies","devDependencies","peerDependencies"):
        section=package.get(key,{})
        if isinstance(section,dict):deps.update(section)
    py=metadata(root/"pyproject.toml",True)
    project=py.get("project",{})
    if not isinstance(project,dict):project={}
    pydeps=project.get("dependencies",[])
    if not isinstance(pydeps,list):pydeps=[]
    dependencies={re.split(r"[<>=!~;\[ ]",d.lower())[0] for d in pydeps if isinstance(d,str)}
    poetry=py.get("tool",{}).get("poetry",{}) if isinstance(py.get("tool",{}),dict) else {}
    if isinstance(poetry,dict) and isinstance(poetry.get("dependencies"),dict):dependencies.update(poetry["dependencies"])
    languages=sorted({EXTENSIONS[s] for s in suffixes if s in EXTENSIONS})
    def add(language):
        if language not in languages:languages.append(language)
    if "tsconfig.json" in names:add("TypeScript")
    if py or "requirements.txt" in names:add("Python")
    if "cargo.toml" in names:add("Rust")
    if "go.mod" in names:add("Go")
    if "package.swift" in names:add("Swift")
    if package and not {"JavaScript","TypeScript"}.intersection(languages):add("JavaScript")
    kind,evidence="Unclassified",[]
    if ".qext" in suffixes or any(d.startswith("@nebula.js/") for d in deps):
        kind,evidence="Qlik extension",["Qlik extension metadata or package dependency"]
    elif any(n.endswith((".xcodeproj",".xcworkspace",".app")) for n in names) or {"electron","@tauri-apps/api"}.intersection(deps) or "tauri.conf.json" in names or "pubspec.yaml" in names:
        kind,evidence="Native app",["Native application project or runtime"]
    elif {"next","nuxt","@sveltejs/kit","@angular/core"}.intersection(deps):
        kind,evidence="Web app",["Web application framework dependency"]
    elif {"@11ty/eleventy","astro","vitepress","gatsby","vuepress"}.intersection(deps) or {"hugo.toml","hugo.yaml","_config.yml","mkdocs.yml"}.intersection(names):
        kind,evidence="Website",["Site generator dependency or configuration"]
    elif {"fastapi","flask","django","starlette"}.intersection(dependencies) or {"express","fastify","koa","@nestjs/core","hono"}.intersection(deps):
        kind,evidence="API / service",["Server framework dependency"]
    elif package.get("bin") or project.get("scripts") or (isinstance(poetry,dict) and poetry.get("scripts")) or "src/main.rs" in paths or "main.go" in paths:
        kind,evidence="Command-line tool",["Executable entry point in project metadata or standard layout"]
    elif package.get("exports"):
        kind,evidence="Library / package",["Exported package entry points"]
    elif {"react","vue","svelte","vite"}.intersection(deps):
        kind,evidence="Web app",["Browser UI framework or bundler dependency"]
    elif ".ipynb" in suffixes:
        kind,evidence="Data / notebooks",["Jupyter notebook files"]
    elif "index.html" in paths:
        kind,evidence="Website",["HTML site entry point"]
    elif package.get("exports") or package.get("main") or py or "cargo.toml" in names or "go.mod" in names or "package.swift" in names:
        kind,evidence="Library / package",["Package manifest or exported module entry point"]
    elif {".py",".sh",".zsh"}.intersection(suffixes):
        kind,evidence="Scripts / automation",["Script files without a recognized application manifest"]
    if not evidence:evidence=["No recognized project-purpose metadata"]
    return {"projectType":kind,"languages":sorted(languages),"typeEvidence":evidence}
