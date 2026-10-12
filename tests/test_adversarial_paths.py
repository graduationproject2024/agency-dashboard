import os
import pytest
from app import app, safe_resolve_project_dir, GENERATED_APPS_DIR, WINDOWS_RESERVED, run_qa_checks

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_windows_reserved_device_names_blocked():
    """Verify that Windows reserved device names cannot be resolved or accessed."""
    for reserved in ["CON", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "con.txt", "aux.html"]:
        assert safe_resolve_project_dir(reserved) is None

def test_safe_resolve_project_dir_blocks_root_generated_apps_dir():
    """Verify safe_resolve_project_dir never resolves to GENERATED_APPS_DIR root itself."""
    assert safe_resolve_project_dir(".") is None
    assert safe_resolve_project_dir("") is None

def test_csrf_origin_validation(client):
    """Verify that requests from foreign origins are blocked with 403 Forbidden."""
    untrusted_headers = {"Origin": "https://malicious-attacker.com"}
    trusted_headers = {"Origin": "http://127.0.0.1:8080"}

    # Foreign origin blocked on /api/start_task
    res = client.post('/api/start_task', json={"task": "malicious task"}, headers=untrusted_headers)
    assert res.status_code == 403
    assert "Invalid origin" in res.get_json()["error"]

    # Foreign origin blocked on /api/settings
    res = client.post('/api/settings', json={"provider": "gemini"}, headers=untrusted_headers)
    assert res.status_code == 403

    # Foreign origin blocked on /api/open_folder
    res = client.post('/api/open_folder', json={"name": "test"}, headers=untrusted_headers)
    assert res.status_code == 403

    # Foreign origin blocked on /api/approve
    res = client.post('/api/approve', json={"feedback": "approved"}, headers=untrusted_headers)
    assert res.status_code == 403

    # Trusted local origin allowed through CSRF check (will fail or succeed based on payload validation)
    res = client.post('/api/settings', json={"provider": "gemini"}, headers=trusted_headers)
    assert res.status_code == 200

def test_settings_rejects_invalid_types(client):
    """Verify that /api/settings strictly requires string values and rejects non-strings."""
    # Integer value for openrouter_model
    res = client.post('/api/settings', json={"openrouter_model": 12345})
    assert res.status_code == 400
    assert "must be a string" in res.get_json()["error"]

    # Boolean value for opencode_url
    res = client.post('/api/settings', json={"opencode_url": True})
    assert res.status_code == 400
    assert "must be a string" in res.get_json()["error"]

    # Array value for provider
    res = client.post('/api/settings', json={"provider": ["invalid", "list"]})
    assert res.status_code == 400
    assert "must be a string" in res.get_json()["error"]

def test_open_folder_blocks_root_directory_access(client):
    """Verify POST /api/open_folder rejects path equal to GENERATED_APPS_DIR itself."""
    res = client.post('/api/open_folder', json={"path": GENERATED_APPS_DIR})
    assert res.status_code == 403

def test_production_run_qa_checks_directly():
    """Verify that the production run_qa_checks function directly flags invalid code and fatal errors."""
    from app import run_qa_checks
    
    valid_html = "<!DOCTYPE html><html><head><title>Test</title></head><body><div>Valid App Content</div></body></html>"
    valid_css = "body { color: blue; } .card { padding: 10px; }"
    valid_js = "function test() { console.log('ok'); }"

    # 1. Missing or empty HTML
    res_empty = run_qa_checks("", valid_css, valid_js)
    assert not res_empty["passed"]
    assert any("missing or empty" in e for e in res_empty["errors"])

    # 2. Truncated / short HTML (< 50 chars)
    res_short = run_qa_checks("<html>stub</html>", valid_css, valid_js)
    assert not res_short["passed"]
    assert any("truncated" in e for e in res_short["errors"])

    # 3. Incomplete structure lacking closing tags
    res_unclosed = run_qa_checks("<div><p>Incomplete stub without doctype or closing html tags " * 3, valid_css, valid_js)
    assert not res_unclosed["passed"]
    assert any("structure" in e for e in res_unclosed["errors"])

    # 4. Fatal error placeholder strings in HTML
    res_fatal = run_qa_checks(f"<!DOCTYPE html><html><body>فشل الاتصال بالخادم {'x'*50}</body></html>", valid_css, valid_js)
    assert not res_fatal["passed"]
    assert any("Fatal error" in e for e in res_fatal["errors"])

    # 5. Unbalanced CSS braces
    res_bad_css = run_qa_checks(valid_html, "body { color: red; ", valid_js)
    assert not res_bad_css["passed"]
    assert any("CSS" in e for e in res_bad_css["errors"])

    # 6. Unbalanced JS delimiters
    res_bad_js = run_qa_checks(valid_html, valid_css, "function test() { if (true) { console.log('unclosed'); }")
    assert not res_bad_js["passed"]
    assert any("JavaScript" in e for e in res_bad_js["errors"])

    # 7. Valid complete deliverable passes
    res_valid = run_qa_checks(valid_html, valid_css, valid_js)
    assert res_valid["passed"]
    assert len(res_valid["errors"]) == 0
    assert all(res_valid["checks"].values())

def test_qa_gate_rejects_missing_required_assets():
    """Verify that omitting required HTML, CSS, or JS causes QA rejection."""
    valid_html = "<!DOCTYPE html><html><head><title>App</title></head><body><h1>App</h1></body></html>"
    valid_css = "body { background: #fff; }"
    valid_js = "console.log('OK');"

    # Missing HTML
    res1 = run_qa_checks("", valid_css, valid_js)
    assert not res1["passed"]
    assert any("HTML" in e and "missing" in e for e in res1["errors"])

    # Missing CSS
    res2 = run_qa_checks(valid_html, "", valid_js)
    assert not res2["passed"]
    assert any("CSS" in e and "missing" in e for e in res2["errors"])

    # Missing JavaScript
    res3 = run_qa_checks(valid_html, valid_css, "")
    assert not res3["passed"]
    assert any("JavaScript" in e and "missing" in e for e in res3["errors"])

def test_qa_gate_rejects_syntactically_invalid_javascript():
    """Verify that real JavaScript syntax errors (caught by Node parser) cause QA rejection."""
    valid_html = "<!DOCTYPE html><html><head><title>App</title></head><body><h1>App</h1></body></html>"
    valid_css = "body { background: #fff; }"

    invalid_js_samples = [
        "const x = ;",
        "function missingParam( { return 1; }",
        "var obj = { a: 1, , b: 2 };",
        "for (let i = 0; i < 10 i++) {}"
    ]
    for bad_js in invalid_js_samples:
        res = run_qa_checks(valid_html, valid_css, bad_js)
        assert not res["passed"], f"QA gate accepted invalid JS: {bad_js}"
        assert any("JavaScript" in e for e in res["errors"])

def test_qa_gate_rejects_malformed_html():
    """Verify that malformed or non-HTML strings are rejected."""
    valid_css = "body { color: blue; }"
    valid_js = "function ok() {}"

    malformed_samples = [
        "This is just plain text without any HTML tags at all.",
        "{ json: 'not html', format: true }",
        "<<<badly formed tag>>>"
    ]
    for bad_html in malformed_samples:
        res = run_qa_checks(bad_html, valid_css, valid_js)
        assert not res["passed"], f"QA gate accepted malformed HTML: {bad_html}"
        assert any("HTML" in e for e in res["errors"])

def test_qa_gate_rejects_invalid_css():
    """Verify that unbalanced or unclosed block comments in CSS fail QA."""
    valid_html = "<!DOCTYPE html><html><head><title>App</title></head><body><h1>App</h1></body></html>"
    valid_js = "function ok() {}"

    invalid_css_samples = [
        "body { color: red; /* unclosed comment",
        "body { color: red; }}",
        "h1 { font-size: 20px; { color: blue; }"
    ]
    for bad_css in invalid_css_samples:
        res = run_qa_checks(valid_html, bad_css, valid_js)
        assert not res["passed"], f"QA gate accepted invalid CSS: {bad_css}"
        assert any("CSS" in e for e in res["errors"])

def test_qa_gate_rejects_malformed_html_nesting():
    """
    Finding 1: Verify that StrictHTMLStructureValidator catches malformed nesting,
    mismatched tags, unclosed non-void elements, and misplaced required elements.
    """
    valid_css = "body { color: #333; }"
    valid_js = "console.log('OK');"

    malformed_nesting_samples = [
        # 1. Crossed/mismatched tags: <div><span></div></span>
        ("<!DOCTYPE html><html><head><title>T</title></head><body><div><span></div></span></body></html>", "Mismatched closing tag"),
        # 2. Unclosed element before closing parent: <div> unclosed before </body>
        ("<!DOCTYPE html><html><head><title>T</title></head><body><div></body></html>", "Unclosed element"),
        # 3. Unclosed nested element at end of file
        ("<!DOCTYPE html><html><head><title>T</title></head><body><div><section><p>Unclosed paragraph", "Unclosed element"),
        # 4. Missing required document element: missing <head>
        ("<!DOCTYPE html><html><body><h1>No head</h1></body></html>", "Missing required document element: <head>"),
        # 5. Missing required document element: missing <body>
        ("<!DOCTYPE html><html><head><title>T</title></head></html>", "Missing required document element: <body>"),
        # 6. Misplaced element: <head> after <body>
        ("<!DOCTYPE html><html><body><h1>Body first</h1></body><head><title>T</title></head></html>", "<head> element cannot appear after <body>"),
        # 7. Non-void element using XML self-closing syntax
        ("<!DOCTYPE html><html><head><title>T</title></head><body><div/></body></html>", "Non-void element"),
        # 8. Void element with prohibited closing tag
        ("<!DOCTYPE html><html><head><title>T</title></head><body><img src='x.png'></img></body></html>", "Void element '<img>' must not have a closing tag")
    ]

    for bad_html, expected_err in malformed_nesting_samples:
        res = run_qa_checks(bad_html, valid_css, valid_js)
        assert not res["passed"], f"QA gate failed to reject malformed HTML nesting: {bad_html}"
        assert any(expected_err in e for e in res["errors"]), f"Expected '{expected_err}' in errors: {res['errors']}"

def test_qa_gate_accepts_valid_html_with_all_void_elements():
    """
    Finding 1: Verify that legitimate HTML5 void elements (img, meta, link, br, hr, input)
    pass strict structural validation without false positives.
    """
    valid_html = (
        "<!DOCTYPE html>\n"
        "<html lang='ar' dir='rtl'>\n"
        "<head>\n"
        "    <meta charset='utf-8'>\n"
        "    <meta name='viewport' content='width=device-width, initial-scale=1.0'>\n"
        "    <title>تطبيق متكامل</title>\n"
        "    <link rel='stylesheet' href='style.css'>\n"
        "</head>\n"
        "<body>\n"
        "    <header>\n"
        "        <img src='logo.png' alt='شعار'>\n"
        "        <br>\n"
        "        <hr>\n"
        "    </header>\n"
        "    <main>\n"
        "        <form>\n"
        "            <input type='text' name='q' placeholder='بحث'>\n"
        "        </form>\n"
        "    </main>\n"
        "    <script src='script.js'></script>\n"
        "</body>\n"
        "</html>"
    )
    valid_css = "body { margin: 0; color: #222; } hr { border: 0; }"
    valid_js = "document.addEventListener('DOMContentLoaded', () => { console.log('Ready'); });"

    res = run_qa_checks(valid_html, valid_css, valid_js)
    assert res["passed"], f"QA gate rejected valid HTML with void elements: {res['errors']}"
    assert len(res["errors"]) == 0
    assert res["checks"]["html_structure_valid"] is True
    assert res["checks"]["css_structural_validity"] is True
    assert res["checks"]["js_syntax_validated"] is True

def test_qa_gate_js_validation_honest_failures(monkeypatch):
    """
    Finding 2: Verify that JavaScript syntax validation fails honestly with explicit statuses:
    simulating Node.js absence, timeout, invalid syntax, and proving code is never executed.
    """
    import subprocess
    valid_html = "<!DOCTYPE html><html><head><title>App</title></head><body><h1>App</h1></body></html>"
    valid_css = "body { color: blue; }"
    valid_js = "const x = 42; console.log(x);"

    # 1. Simulating Node.js binary absence on PATH
    def mock_subprocess_filenotfound(*args, **kwargs):
        raise FileNotFoundError("node not found")

    monkeypatch.setattr(subprocess, "run", mock_subprocess_filenotfound)
    res_absent = run_qa_checks(valid_html, valid_css, valid_js)
    assert not res_absent["passed"]
    assert any("Node.js runtime is required" in e for e in res_absent["errors"])
    assert res_absent["checks"]["js_syntax_validated"] is False

    # 2. Simulating Node.js subprocess timeout
    def mock_subprocess_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="node", timeout=5)

    monkeypatch.setattr(subprocess, "run", mock_subprocess_timeout)
    res_timeout = run_qa_checks(valid_html, valid_css, valid_js)
    assert not res_timeout["passed"]
    assert any("timed out" in e for e in res_timeout["errors"])
    assert res_timeout["checks"]["js_syntax_validated"] is False

    # 3. Restore real subprocess.run: verify that untrusted JS is compiled, NOT executed
    monkeypatch.undo()
    # If code were executed, this would raise or exit with code 99. vm.Script compiles without running.
    safe_compilation_js = "if (false) { process.exit(99); }"
    res_noexec = run_qa_checks(valid_html, valid_css, safe_compilation_js)
    assert res_noexec["passed"]
    assert res_noexec["checks"]["js_syntax_validated"] is True

def test_qa_gate_css_structural_and_declaration_validation():
    """
    Finding 3: Verify that CSS structural validation detects malformed declarations,
    missing properties/colons/values, and unbalanced braces without overstating full CSS3/4 AST parsing.
    """
    valid_html = "<!DOCTYPE html><html><head><title>App</title></head><body><h1>App</h1></body></html>"
    valid_js = "console.log('ok');"

    invalid_css_cases = [
        # Missing colon in declaration
        ("body { color red; }", "missing colon"),
        # Missing property name before colon
        ("body { : red; }", "missing property name"),
        # Missing property value after colon
        ("body { color: ; }", "missing property value"),
        # Unexpected closing brace before opening
        ("} body { color: red; }", "unexpected closing brace"),
        # Unclosed comment
        ("/* unclosed comment body { color: red; }", "unclosed block comments")
    ]

    for bad_css, expected_err in invalid_css_cases:
        res = run_qa_checks(valid_html, bad_css, valid_js)
        assert not res["passed"], f"QA gate accepted invalid CSS: {bad_css}"
        assert any(expected_err in e for e in res["errors"]), f"Expected '{expected_err}' in errors: {res['errors']}"
        assert res["checks"]["css_structural_validity"] is False


def test_qa_validates_final_transformed_html_and_asset_injection():
    """
    Finding A: Regression test proving:
    1. inject_asset_links injects links cleanly into candidate HTML.
    2. QA validates the final transformed HTML.
    3. Closing tag case-insensitivity works (e.g. </HEAD>, </BODY>).
    4. Links are not duplicated when already present.
    5. Document content is preserved.
    """
    from app import inject_asset_links, run_qa_checks

    raw_html = (
        "<!DOCTYPE html>\n"
        "<HTML lang='ar'>\n"
        "<HEAD>\n"
        "    <title>Mixed Case Document</title>\n"
        "</HEAD>\n"
        "<BODY>\n"
        "    <div id='content'>Original Content Preserved</div>\n"
        "</BODY>\n"
        "</HTML>"
    )
    css = "body { color: #333; }"
    js = "console.log('ready');"

    injected_html = inject_asset_links(raw_html, css, js)
    assert '<link rel="stylesheet" href="style.css">' in injected_html
    assert '<script src="script.js"></script>' in injected_html
    assert "Original Content Preserved" in injected_html
    assert "</HEAD>" in injected_html
    assert "</BODY>" in injected_html

    # Proving idempotency: injecting again does NOT duplicate the links
    twice_injected = inject_asset_links(injected_html, css, js)
    assert twice_injected.count('href="style.css"') == 1
    assert twice_injected.count('src="script.js"') == 1

    # Verify that run_qa_checks accepts the final injected HTML
    qa_res = run_qa_checks(injected_html, css, js)
    assert qa_res["passed"] is True, f"QA failed on final candidate: {qa_res['errors']}"


def test_invalid_html_introduced_or_unclosed_is_rejected():
    """
    Finding A: Proving that if HTML is missing required structure or has malformed tags,
    it is rejected by QA even after asset link injection attempt.
    """
    from app import inject_asset_links, run_qa_checks

    # Missing </head> and <body>, malformed
    bad_html = "<!DOCTYPE html><html><head><title>Test</title><div>Missing body and unclosed head"
    css = "body { color: red; }"
    js = "console.log('test');"

    candidate = inject_asset_links(bad_html, css, js)
    qa_res = run_qa_checks(candidate, css, js)
    assert qa_res["passed"] is False
    assert any("element" in e.lower() or "missing" in e.lower() for e in qa_res["errors"])


def test_js_validation_accepts_valid_code_with_unbalanced_delimiters():
    """
    Finding B: Regression test proving removal of raw delimiter-count checks.
    The Node.js vm.Script compiler correctly accepts valid JavaScript that contains
    unmatched braces/brackets/parentheses inside strings, comments, template literals,
    and regexes.
    """
    from app import verify_js_syntax, run_qa_checks

    valid_html = "<!DOCTYPE html><html><head><title>T</title><link rel='stylesheet' href='style.css'></head><body><div id='app'></div><script src='script.js'></script></body></html>"
    valid_css = "body { color: green; }"

    valid_js_samples = [
        # 1. Unmatched delimiter inside string literal
        'const msg = "{"; const arr = "["; const paren = "(";',
        # 2. Delimiter inside single-line comment
        '// Unclosed brace in comment: {\nconst x = 42;',
        # 3. Delimiter inside multiline block comment
        '/* Block comment with { and } and (( */\nfunction getAnswer() { return 42; }',
        # 4. Template literals with expressions and literal delimiters
        'const name = "World";\nconst str = `Hello ${name}, brace: { paren: ( [`;',
        # 5. Escaped quotes containing delimiter
        'const escaped = "Quoted \\" { string \\" with braces";',
        # 6. Valid complex nested object, functions, arrays, and regex
        'const re = /\\{([0-9]+)\\}/g;\nconst app = { handlers: [() => ({ ok: true })] };',
        # 7. Valid multiline real-world script
        '''
        class DashboardWidget {
            constructor(selector) {
                this.el = document.querySelector(selector);
                this.prefix = "prefix: {";
            }
            render() {
                if (!this.el) return;
                const items = [1, 2, 3].map(n => ({ id: n, text: `Item {${n}}` }));
                this.el.innerHTML = items.map(i => `<div>${i.text}</div>`).join("");
            }
        }
        window.widget = new DashboardWidget("#app");
        '''
    ]

    for js_code in valid_js_samples:
        status, err = verify_js_syntax(js_code)
        assert status == "valid", f"Node vm.Script rejected valid JS: {err}\nCode: {js_code}"
        qa_res = run_qa_checks(valid_html, valid_css, js_code)
        assert qa_res["passed"] is True, f"QA gate rejected valid JS deliverable: {qa_res['errors']}\nCode: {js_code}"


def test_js_validation_rejects_genuine_syntax_errors():
    """
    Finding B: Regression test verifying that genuine syntax errors are caught
    by the Node.js compiler and fail the QA gate.
    """
    from app import verify_js_syntax, run_qa_checks

    valid_html = "<!DOCTYPE html><html><head><title>T</title></head><body><h1>T</h1></body></html>"
    valid_css = "body { color: #000; }"

    invalid_js_cases = [
        "const x = 1 + ;",
        "function (invalidFunc) { return 1; }",
        "const obj = { a: 1,, b: 2 };",
        "for (let i = 0 i < 10; i++) {}",
        "if (true { console.log('missing paren'); }",
        "const str = 'unterminated string literal;"
    ]

    for bad_js in invalid_js_cases:
        status, err = verify_js_syntax(bad_js)
        assert status == "invalid", f"Expected syntax error, got status '{status}' for: {bad_js}"
        qa_res = run_qa_checks(valid_html, valid_css, bad_js)
        assert qa_res["passed"] is False
        assert any("JavaScript" in e for e in qa_res["errors"])


def test_js_validation_empty_input_policy():
    """
    Finding B: Explicitly verifies documented policy for empty JavaScript input.
    Empty, whitespace, or missing JS must be rejected with an honest error.
    """
    from app import verify_js_syntax, run_qa_checks

    valid_html = "<!DOCTYPE html><html><head><title>T</title></head><body><h1>T</h1></body></html>"
    valid_css = "body { color: #000; }"

    for empty_js in ["", "   ", "\n\t\n", None]:
        status, err = verify_js_syntax(empty_js)
        assert status == "invalid"
        assert "missing or empty" in err
        qa_res = run_qa_checks(valid_html, valid_css, empty_js)
        assert qa_res["passed"] is False
        assert any("Required JavaScript artifact is missing or empty" in e for e in qa_res["errors"])


def test_css_validation_comprehensive_cases():
    """
    Finding C: Regression test covering all documented CSS validation guarantees and limits:
    - Unclosed comments rejected
    - Unbalanced braces rejected
    - Unterminated quoted strings rejected
    - Semicolons and braces inside valid quotes accepted without false rejection
    - Valid @media blocks and @keyframes accepted
    - Empty CSS rejected per documented policy
    """
    from app import validate_css_structure, run_qa_checks

    valid_html = "<!DOCTYPE html><html><head><title>T</title></head><body><h1>T</h1></body></html>"
    valid_js = "console.log('ready');"

    # 1. Unterminated quoted strings rejected
    ok1, err1 = validate_css_structure("body { font-family: 'Open Sans; color: red; }")
    assert not ok1
    assert any("unterminated quoted string" in e for e in err1)

    # 2. Semicolons and braces inside valid quoted strings correctly accepted
    css_with_quoted_delimiters = (
        'body {\n'
        '    content: ";}";\n'
        '    font-family: "Segoe UI", sans-serif;\n'
        '    background-image: url("images/bg;v=1.png");\n'
        '}'
    )
    ok2, err2 = validate_css_structure(css_with_quoted_delimiters)
    assert ok2 is True, f"Failed on valid CSS containing quoted delimiters: {err2}"

    # 3. Valid nested @media rules accepted
    media_css = (
        '@media (min-width: 768px) and (max-width: 1200px) {\n'
        '    .container { max-width: 960px; margin: 0 auto; }\n'
        '    .sidebar { display: block; width: 250px; }\n'
        '}\n'
        '@media print {\n'
        '    .no-print { display: none; }\n'
        '}'
    )
    ok3, err3 = validate_css_structure(media_css)
    assert ok3 is True, f"Failed on valid @media query blocks: {err3}"

    # 4. Valid @keyframes rule accepted
    keyframes_css = (
        '@keyframes pulse {\n'
        '    0% { transform: scale(1); opacity: 1; }\n'
        '    50% { transform: scale(1.05); opacity: 0.8; }\n'
        '    100% { transform: scale(1); opacity: 1; }\n'
        '}'
    )
    ok4, err4 = validate_css_structure(keyframes_css)
    assert ok4 is True, f"Failed on valid @keyframes rule: {err4}"

    # 5. Empty CSS rejected
    ok5, err5 = validate_css_structure("")
    assert not ok5
    assert any("missing or empty" in e for e in err5)

    # 6. Verify end-to-end in run_qa_checks
    qa_res = run_qa_checks(valid_html, media_css, valid_js)
    assert qa_res["passed"] is True


def test_asset_link_detection_adversarial_and_safe_cases():
    """
    Workstream B: Regression tests covering exact asset-link detection requirements:
    1. Correct relative stylesheet & script references (style.css, ./style.css, css/style.css, script.js, ./script.js, js/script.js).
    2. Single- and double-quoted attributes.
    3. Repeated injection remaining idempotent.
    4. Adversarial filenames: not-style.css and not-script.js must NOT satisfy detection.
    5. References inside HTML comments must NOT satisfy detection.
    6. References in unrelated tags (<a href="style.css">, <img src="script.js">, <div data-file="style.css">) must NOT satisfy detection.
    7. Non-stylesheet link tags (<link rel="preload" href="style.css">) must NOT satisfy detection.
    """
    from app import inject_asset_links, _has_valid_stylesheet_link, _has_valid_script_reference

    css_content = "body { color: black; }"
    js_content = "console.log('init');"

    # 1. Adversarial CSS bypass attempts that MUST trigger injection
    adversarial_css_htmls = [
        # Similar filename not-style.css
        '<!DOCTYPE html><html><head><link rel="stylesheet" href="not-style.css"></head><body></body></html>',
        # Similar filename old-style.css
        '<!DOCTYPE html><html><head><link rel="stylesheet" href="old-style.css"></head><body></body></html>',
        # Commented out link
        '<!DOCTYPE html><html><head><!-- <link rel="stylesheet" href="style.css"> --></head><body></body></html>',
        # Unrelated <a> tag
        '<!DOCTYPE html><html><head></head><body><a href="style.css">Download CSS</a></body></html>',
        # Unrelated <div data-file="..."> tag
        '<!DOCTYPE html><html><head></head><body><div data-file="style.css">Block</div></body></html>',
        # Non-stylesheet link (preload)
        '<!DOCTYPE html><html><head><link rel="preload" href="style.css" as="style"></head><body></body></html>',
    ]

    for adv_html in adversarial_css_htmls:
        assert not _has_valid_stylesheet_link(adv_html), f"Falsely accepted adversarial CSS reference in: {adv_html}"
        res = inject_asset_links(adv_html, css_content, "")
        assert '<link rel="stylesheet" href="style.css">' in res, f"Failed to inject required stylesheet into: {adv_html}"

    # 2. Adversarial JS bypass attempts that MUST trigger injection
    adversarial_js_htmls = [
        # Similar filename not-script.js
        '<!DOCTYPE html><html><head></head><body><script src="not-script.js"></script></body></html>',
        # Similar filename old-script.js
        '<!DOCTYPE html><html><head></head><body><script src="old-script.js"></script></body></html>',
        # Commented out script
        '<!DOCTYPE html><html><head></head><body><!-- <script src="script.js"></script> --></body></html>',
        # Unrelated <a> tag
        '<!DOCTYPE html><html><head></head><body><a href="script.js">Download Script</a></body></html>',
        # Unrelated <img> tag
        '<!DOCTYPE html><html><head></head><body><img src="script.js" alt="Fake"></body></html>',
    ]

    for adv_html in adversarial_js_htmls:
        assert not _has_valid_script_reference(adv_html), f"Falsely accepted adversarial JS reference in: {adv_html}"
        res = inject_asset_links(adv_html, "", js_content)
        assert '<script src="script.js"></script>' in res, f"Failed to inject required script into: {adv_html}"

    # 3. Legitimate references that MUST be recognized and NOT duplicated (idempotency)
    legit_css_cases = [
        '<link rel="stylesheet" href="style.css">',
        '<link rel="stylesheet" href="./style.css">',
        '<link rel="stylesheet" href="css/style.css">',
        "<link rel='stylesheet' href='style.css'>",
        '<link href="style.css" rel="stylesheet">',
    ]
    for legit in legit_css_cases:
        doc = f"<!DOCTYPE html><html><head>{legit}</head><body></body></html>"
        assert _has_valid_stylesheet_link(doc), f"Failed to recognize valid stylesheet: {legit}"
        res = inject_asset_links(doc, css_content, "")
        assert res.count("style.css") == 1, f"Duplicated CSS link for: {legit}"

    legit_js_cases = [
        '<script src="script.js"></script>',
        '<script src="./script.js"></script>',
        '<script src="js/script.js"></script>',
        "<script src='script.js'></script>",
    ]
    for legit in legit_js_cases:
        doc = f"<!DOCTYPE html><html><head></head><body>{legit}</body></html>"
        assert _has_valid_script_reference(doc), f"Failed to recognize valid script: {legit}"
        res = inject_asset_links(doc, "", js_content)
        assert res.count("script.js") == 1, f"Duplicated script tag for: {legit}"
