import asyncio
import json
import re
import os
import traceback
from time import sleep

import nodriver as uc
import datetime


def _dbg():
    return str(os.getenv('WB_DEBUG') or os.getenv('DEBUG') or '').lower() in (
        '1', 'true', 'yes', 'on'
    )


def _log(label, obj=None):
    if not _dbg():
        return
    try:
        if obj is None:
            print(f"[WB-BROWSER] {label}")
        else:
            print(f"[WB-BROWSER] {label}: {obj}")
    except Exception:
        pass


def _get_loop():
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop


async def _page_eval(page, script):
    try:
        return await page.evaluate(script)
    except Exception:
        return None


async def _collect_storage(page, browser=None):
    ls = await _page_eval(page, 'JSON.stringify(Object.fromEntries(Object.entries(localStorage)))')
    ss = await _page_eval(page, 'JSON.stringify(Object.fromEntries(Object.entries(sessionStorage)))')
    cookies_list = []
    # Prefer browser-level cookies API (nodriver)
    if browser is not None:
        try:
            cookies = await browser.cookies.get_all()
            cookies_list = [{"name": c.name, "value": c.value} for c in cookies]
        except Exception:
            try:
                # Fallback via page context if available
                cookies = await page.context.browser.cookies.get_all()
                cookies_list = [{"name": c.name, "value": c.value} for c in cookies]
            except Exception:
                pass
    try:
        ls_dict = json.loads(ls) if ls else {}
    except Exception:
        ls_dict = {}
    try:
        ss_dict = json.loads(ss) if ss else {}
    except Exception:
        ss_dict = {}
    return ls_dict, ss_dict, cookies_list


async def _maybe_handle_otp(page):
    # Detect OTP prompt after password submit and enter code if provided
    try:
        # Try to detect a code input: either a single input[type=number] or 6 separate digit inputs
        selectors = [
            'input[type=number]',
            'input[placeholder*="code" i]',
            'input[aria-label*="code" i]',
        ]
        code_input = None
        for sel in selectors:
            try:
                el = await page.select(sel)
                if el:
                    code_input = el
                    break
            except Exception:
                continue

        # If there are multiple one-char inputs, fill them sequentially
        multi_digit_inputs = []
        if not code_input:
            try:
                inputs = await page.select('input')
                if inputs and isinstance(inputs, list):
                    # Heuristic: 4-8 inputs with maxlength=1
                    for el in inputs:
                        try:
                            is_pwd_digit = await page.evaluate('(e)=>e && e.getAttribute && e.getAttribute("maxlength")=="1"', el)
                        except Exception:
                            is_pwd_digit = False
                        if is_pwd_digit:
                            multi_digit_inputs.append(el)
                    if len(multi_digit_inputs) >= 4:
                        _log('otp.multi_inputs.detected', len(multi_digit_inputs))
            except Exception:
                pass

        # Try to find a 'Send code' / 'Get code' button on OTP screen
        send_btn = None
        try:
            for label in ["Get code", "Send code", "Resend", "Send", "Send Code"]:
                try:
                    b = await page.find(label, best_match=True)
                    if b:
                        send_btn = b
                        break
                except Exception:
                    continue
        except Exception:
            pass

        if code_input or multi_digit_inputs:
            code = os.getenv('WEBULL_LOGIN_CODE')
            if not code:
                # Still try to trigger sending a code so the user can enter manually when headless=false
                try:
                    if send_btn:
                        await send_btn.click()
                        _log('otp.sent')
                except Exception:
                    pass
                _log('otp.required.no_code_env')
                return False
            _log('otp.enter')
            if code_input:
                try:
                    await code_input.click()
                except Exception:
                    pass
                await code_input.send_keys(code)
            else:
                # Fill one char per input
                for i, ch in enumerate(code.strip()):
                    if i >= len(multi_digit_inputs):
                        break
                    try:
                        await multi_digit_inputs[i].click()
                    except Exception:
                        pass
                    await multi_digit_inputs[i].send_keys(ch)
            # Click Verify/Continue
            try:
                for label in ["Verify", "Continue", "Confirm", "Submit", "Login"]:
                    try:
                        btn = await page.find(label, best_match=True)
                        if btn:
                            await btn.click()
                            break
                    except Exception:
                        continue
            except Exception:
                pass
            await page.context.browser.sleep(3)
            return True
    except Exception:
        pass
    return False


def _extract_tokens(ls_dict, ss_dict, cookies_list=None):
    # Common token keys observed in Webull local/session storage
    keys = ['accessToken', 'refreshToken', 'uuid', 'tokenExpireTime', 'access_token', 'refresh_token']
    found = {k: None for k in keys}
    # direct keys
    for k in keys:
        if k in ls_dict and ls_dict[k]:
            found[k] = ls_dict[k]
        if k in ss_dict and not found[k] and ss_dict[k]:
            found[k] = ss_dict[k]
    # nested JSON values and generic token scanning
    def scan_obj(o):
        if isinstance(o, dict):
            for kk, vv in o.items():
                if kk in keys and vv:
                    found[kk] = vv
                scan_obj(vv)
        elif isinstance(o, list):
            for item in o:
                scan_obj(item)
    for src in [ls_dict, ss_dict]:
        for v in src.values():
            try:
                if isinstance(v, str) and (v.strip().startswith('{') or v.strip().startswith('[')):
                    scan_obj(json.loads(v))
                elif isinstance(v, str) and 'token' in v.lower():
                    # Regex search for token fields inside string blobs
                    for m in re.finditer(r'"(accessToken|access_token)"\s*:\s*"([^"]+)"', v, re.IGNORECASE):
                        found['accessToken'] = found.get('accessToken') or m.group(2)
                        found['access_token'] = found.get('access_token') or m.group(2)
                    for m in re.finditer(r'"(refreshToken|refresh_token)"\s*:\s*"([^"]+)"', v, re.IGNORECASE):
                        found['refreshToken'] = found.get('refreshToken') or m.group(2)
                        found['refresh_token'] = found.get('refresh_token') or m.group(2)
                    for m in re.finditer(r'"uuid"\s*:\s*"([^"]+)"', v, re.IGNORECASE):
                        found['uuid'] = found.get('uuid') or m.group(1)
                    for m in re.finditer(r'"tokenExpireTime"\s*:\s*"([^"]+)"', v, re.IGNORECASE):
                        found['tokenExpireTime'] = found.get('tokenExpireTime') or m.group(1)
            except Exception:
                continue
    # Also scan cookies
    if cookies_list:
        cdict = {}
        try:
            cdict = {c.get('name'): c.get('value') for c in cookies_list}
        except Exception:
            pass
        cand_names = ['accessToken','access_token','refreshToken','refresh_token','uuid','u_uuid']
        for cn in cand_names:
            if cn in cdict and cdict[cn]:
                found[cn] = cdict[cn]
    # Return only if we found core tokens
    access = found.get('accessToken') or found.get('access_token')
    refresh = found.get('refreshToken') or found.get('refresh_token')
    if access and refresh:
        return {
            'accessToken': access,
            'refreshToken': refresh,
            'uuid': found.get('uuid'),
            'tokenExpireTime': found.get('tokenExpireTime'),
        }
    # If only access token is available via cookies, still return it
    if access:
        return {
            'accessToken': access,
            'refreshToken': refresh or '',
            'uuid': found.get('uuid'),
            'tokenExpireTime': found.get('tokenExpireTime'),
        }
    return None


async def _perform_login(browser, username, password, login_url=None):
    login_url = (
        login_url
        or os.getenv(
            "WEBULL_LOGIN_URL",
            "https://passport.webull.com/auth/simple/login?redirect_uri=https%3A%2F%2Fapp.webull.com%2Fwatch",
        )
    )
    _log('navigate.login_url', login_url)
    page = await browser.get(login_url)
    if not page:
        raise Exception("Failed to open Webull login page")
    await browser.sleep(3)
    _log('page.opened')

    # Try multiple selectors for username and password inputs
    username_selectors = [
        'input[type=email]',
        'input[type=text]',
        'input[name*="account" i]',
        'input[placeholder*="mail" i]',
        'input[placeholder*="phone" i]',
        'input.csr61:not([type="password"])',
        '#app input.csr61:not([type="password"])',
        '#app > div > div.csr6 > div > div > div.csr33 > div:nth-child(1) input.csr61',
    ]
    password_selectors = [
        'input[type=password]',
        'input[name*="pass" i]',
        'input.csr61[type="password"]',
        '#app > div > div.csr6 > div > div > div.csr33 > div:nth-child(3) > div > span > input',
    ]

    user_el = None
    pass_el = None
    _log('locate.username.start')
    for sel in username_selectors:
        try:
            user_el = await page.select(sel)
            if user_el:
                _log('locate.username.found', sel)
                break
        except Exception:
            continue
    _log('locate.password.start')
    for sel in password_selectors:
        try:
            pass_el = await page.select(sel)
            if pass_el:
                _log('locate.password.found', sel)
                break
        except Exception:
            continue

    if not user_el or not pass_el:
        # Try to switch to password login if currently on code login
        _log('try.switch.password_login')
        try:
            switchers = ["Password", "Use password", "Password login", "Use Password"]
            for label in switchers:
                try:
                    t = await page.find(label, best_match=True)
                    if t:
                        await t.click()
                        await browser.sleep(1)
                        break
                except Exception:
                    continue
        except Exception:
            pass
        # Retry find after switch
        if not user_el:
            for sel in username_selectors:
                try:
                    user_el = await page.select(sel)
                    if user_el:
                        _log('locate.username.found.after_switch', sel)
                        break
                except Exception:
                    continue
        if not pass_el:
            for sel in password_selectors:
                try:
                    pass_el = await page.select(sel)
                    if pass_el:
                        _log('locate.password.found.after_switch', sel)
                        break
                except Exception:
                    continue
        # Some layouts wrap inputs; try first two inputs
        if not user_el or not pass_el:
            inputs = await page.select('input')
            if inputs and isinstance(inputs, list) and len(inputs) >= 2:
                user_el, pass_el = inputs[0], inputs[1]

    if not user_el or not pass_el:
        # Attempt code-based login flow
        _log('fallback.code_login')
        phone_or_email = None
        code_input = None
        get_code_btn = None
        try:
            # Username field
            for sel in [
                'input[type=text]',
                'input[type=tel]',
                'input[placeholder*="mail" i]',
                'input[placeholder*="phone" i]']:
                phone_or_email = await page.select(sel)
                if phone_or_email:
                    break
            # Code field
            for sel in ['input[type=number]', 'input[placeholder*="code" i]']:
                code_input = await page.select(sel)
                if code_input:
                    break
            # Get code button
            try:
                get_code_btn = await page.find("Get code", best_match=True)
            except Exception:
                get_code_btn = await page.find("Send code", best_match=True)
        except Exception:
            pass
        if phone_or_email and code_input and get_code_btn:
            await phone_or_email.send_keys(username)
            await get_code_btn.click()
            await browser.sleep(3)
            login_code = os.getenv('WEBULL_LOGIN_CODE')
            if not login_code:
                raise Exception("Verification code required. Set WEBULL_LOGIN_CODE to proceed.")
            await code_input.send_keys(login_code)
            # Try to submit
            try:
                submit = await page.find("Login", best_match=True)
            except Exception:
                submit = await page.select('button[type=submit]')
            if submit:
                await submit.click()
                await browser.sleep(5)
        else:
            raise Exception("Unable to locate login inputs (password or code)")

    try:
        await user_el.click()
    except Exception:
        pass
    await user_el.send_keys(username)
    await browser.sleep(0.5)
    try:
        await pass_el.click()
    except Exception:
        pass
    await pass_el.send_keys(password)

    # Click login
    login_labels = ["Sign In", "Log In", "Login", "Sign in", "LOG IN", "Continue", "Next"]
    btn = None
    for label in login_labels:
        try:
            btn = await page.find(label, best_match=True)
            if btn:
                break
        except Exception:
            continue
    if not btn:
        # Try button element
        try:
            btn = await page.select('button[type=submit]')
        except Exception:
            btn = None
    if not btn:
        # Try pressing Enter in password field as a last resort
        try:
            await pass_el.send_keys("\n")
        except Exception:
            pass
    else:
        await btn.click()
    _log('login.clicked')
    # After login, handle OTP if present
    try:
        handled = await _maybe_handle_otp(page)
        if handled:
            _log('otp.handled')
    except Exception as e:
        _log('otp.handler.error', str(e))
    # Wait for redirect into app; then force app route if not redirected
    for _ in range(10):
        await browser.sleep(1)
        try:
            url_now = await page.evaluate("location.href")
            if url_now and ("app.webull.com" in url_now):
                break
        except Exception:
            pass
    try:
        url_now = await page.evaluate("location.href")
    except Exception:
        url_now = None
    if not url_now or "app.webull.com" not in (url_now or ""):
        page = await browser.get("https://app.webull.com/watch")
        await browser.sleep(3)
    
    return page


async def _dump_debug(page, reason="unknown"):
    try:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        html = await _page_eval(page, 'document.documentElement.outerHTML')
        fname = f"webull-login-{reason}-{ts}.html"
        if html:
            with open(fname, 'w', encoding='utf-8') as f:
                f.write(html)
        try:
            await page.save_screenshot(filename=f"webull-login-{reason}-{ts}.png", full_page=True)
        except Exception:
            pass
        _log('debug.dump.saved', {"html": fname})
    except Exception:
        pass


def webull_browser_fetch_tokens(username: str, password: str, headless=True, login_url=None):
    loop = _get_loop()
    browser = None
    try:
        args = [
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        ]
        if headless:
            args.append("--headless=new")
        browser = loop.run_until_complete(uc.start(browser_args=args))
        _log('browser.start')
        try:
            page = loop.run_until_complete(_perform_login(browser, username, password, login_url=login_url))
        except Exception as e:
            _log('perform_login.error', str(e))
            # Try alternate login URL
            try:
                alt_url = "https://app.webull.com/login"
                _log('navigate.alt_login_url', alt_url)
                page = loop.run_until_complete(browser.get(alt_url))
                loop.run_until_complete(browser.sleep(3))
                awaitable = _perform_login(browser, username, password, login_url=alt_url)
                page = loop.run_until_complete(awaitable)
            except Exception as e2:
                try:
                    if 'page' in locals() and page:
                        loop.run_until_complete(_dump_debug(page, reason="inputs_not_found"))
                except Exception:
                    pass
                raise e2

        # Wait and attempt to collect storage a few times
        for i in range(60):
            ls, ss, cookies = loop.run_until_complete(_collect_storage(page, browser))
            tokens = _extract_tokens(ls, ss, cookies_list=cookies)
            cookie_names = []
            try:
                cookie_names = [c.get('name') for c in cookies][:15]
            except Exception:
                pass
            dbg = {"iter": i, "ls_keys": list(ls.keys())[:10], "ss_keys": list(ss.keys())[:10], "cookie_names": cookie_names}
            # Include snippets of interesting storage values to help debugging
            for k in [
                'login-register.login-info-temp',
                'user-store',
                '1685922:session-data',
            ]:
                if k in ls:
                    v = ls.get(k)
                    if isinstance(v, str):
                        dbg[f"ls[{k}]"] = v[:240]
                if k in ss:
                    v = ss.get(k)
                    if isinstance(v, str):
                        dbg[f"ss[{k}]"] = v[:240]
            _log('storage.try', dbg)
            if tokens:
                _log('tokens.found', {k: ('***' if k != 'tokenExpireTime' else v) for k, v in tokens.items()})
                return tokens
            sleep(2)
        # Final debug dump
        try:
            loop.run_until_complete(_dump_debug(page, reason="no_tokens"))
        except Exception:
            pass
        raise Exception("Tokens not found in storage after login. A screenshot and HTML dump were saved.")
    finally:
        try:
            if browser:
                loop.run_until_complete(browser.sleep(1))
                browser.stop()
        except Exception:
            pass
