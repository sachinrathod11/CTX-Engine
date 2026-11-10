from ctx.index.parser import normalise_route, parse


def test_python_symbols_routes_and_blueprint_prefix():
    src = '''"""Auth API."""
from flask import Blueprint
from ..models.user import User
bp = Blueprint("auth", __name__, url_prefix="/api/auth")

@bp.post("/login")
def login():
    """Log a user in."""
    return 1

class Service:
    def run(self):
        pass

@bp.route("/x/<int:id>", methods=["GET", "DELETE"])
def x(id):
    pass
'''
    pf = parse(src, "python")
    names = {s.name: s for s in pf.symbols}
    assert names["login"].start == 6 and names["login"].end == 9  # decorator included
    assert names["Service.run"].kind == "method"
    assert pf.doc == "Auth API."
    routes = {(r.method, r.path) for r in pf.routes}
    assert ("POST", "/api/auth/login") in routes
    assert ("GET", "/api/auth/x/*") in routes and ("DELETE", "/api/auth/x/*") in routes
    assert ("models.user", 2) in pf.imports


def test_js_component_spans_whole_body_and_fetch_method():
    src = """import { login } from '../api/auth'
export default function LoginForm({ onLoggedIn }) {
  async function go() {
    await fetch('/api/auth/login', {
      method: 'POST',
    })
  }
  return <form onSubmit={go}></form>
}
"""
    pf = parse(src, "javascript")
    comp = next(s for s in pf.symbols if s.name == "LoginForm")
    assert comp.kind == "component" and (comp.start, comp.end) == (2, 9)
    assert ("../api/auth", 0) in pf.imports
    assert [(r.method, r.path, r.role) for r in pf.routes] == [("POST", "/api/auth/login", "calls")]


def test_express_routes_are_definitions():
    src = "const express = require('express')\nconst router = express.Router()\nrouter.get('/users/:id', (req, res) => {})\n"
    pf = parse(src, "javascript")
    assert [(r.method, r.path, r.role) for r in pf.routes] == [("GET", "/users/*", "defines")]


def test_go_and_java():
    go = 'package main\nimport (\n  "fmt"\n  "example.com/app/handlers"\n)\nfunc main() {\n  fmt.Println(1)\n}\n'
    pf = parse(go, "go")
    assert ("example.com/app/handlers", 0) in pf.imports
    assert any(s.name == "main" and s.end == 8 for s in pf.symbols)
    java = ('import com.shop.UserService;\n@RestController\npublic class AuthController {\n'
            '  @PostMapping("/api/login")\n  public String login(String u) {\n    return u;\n  }\n}\n')
    pj = parse(java, "java")
    assert ("com.shop.UserService", 0) in pj.imports
    assert {"AuthController", "AuthController.login"} <= {s.name for s in pj.symbols}
    assert ("POST", "/api/login") in {(r.method, r.path) for r in pj.routes}


def test_normalise_route():
    assert normalise_route("https://api.x.com/v1/users/?a=1") == "/v1/users"
    assert normalise_route("${API}/api/items/{id}") == "/api/items/*"
    assert normalise_route("relative/path") == ""
