import base64
import sqlite3
import os
import uuid
import random
import string
import time
import threading
import requests
import gzip
from io import BytesIO
from functools import lru_cache
from flask import Flask, render_template_string, request, redirect, url_for, session, flash
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = 'nexfund_super_secret_key'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['SESSION_REFRESH_EACH_REQUEST'] = True
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024  # 8MB upload cap

# ── CPU GOVERNOR: keeps average CPU usage near 20% of one core ──
_cpu_target = 0.20
_cpu_start_wall = time.time()

        except Exception as e:
            print("GitHub upload error:", e)
            return None
    return None

def delete_github_file(raw_url):
    if not raw_url or 'raw.githubusercontent.com' not in raw_url:
        return
    try:
        prefix = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/"
        if not raw_url.startswith(prefix):
            return
        path = raw_url[len(prefix):]

        get_resp = github_request(
            'GET',
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}",
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json"
            },
            params={"ref": GITHUB_BRANCH},
            timeout=15
        )
        if get_resp.status_code != 200:
            print("GitHub delete lookup failed:", get_resp.status_code, get_resp.text)
            return

        sha = get_resp.json().get('sha')
        del_resp = github_request(
            'DELETE',
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}",
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json"
            },
            json={
                "message": f"Delete proof {path}",
                "sha": sha,
                "branch": GITHUB_BRANCH
            },
            timeout=15
        )
        if del_resp.status_code not in (200, 201):
            print("GitHub delete failed:", del_resp.status_code, del_resp.text)
    except Exception as e:
        print("GitHub delete error:", e)

def get_settings():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM settings").fetchall()
        return {r['key']: bool(r['value']) for r in rows if not r['key'].startswith('min_withdraw_')}

def get_min_withdrawals():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM settings WHERE key LIKE 'min_withdraw_%'").fetchall()
        return {r['key']: int(r['value']) for r in rows}

def send_notification(username, message, conn=None):
    if conn is not None:
        conn.execute("INSERT INTO notifications (username, message, date, read) VALUES (?, ?, ?, 0)",
                     (username, message, datetime.now().strftime("%Y-%m-%d %H:%M")))
        return
    with get_db() as conn2:
        conn2.execute("INSERT INTO notifications (username, message, date, read) VALUES (?, ?, ?, 0)",
                     (username, message, datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn2.commit()

def broadcast_notification(message, conn=None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    if conn is not None:
        users = conn.execute("SELECT username FROM users").fetchall()
        for u in users:
            conn.execute("INSERT INTO notifications (username, message, date, read) VALUES (?, ?, ?, 0)",
                         (u['username'], message, now))
        return
    with get_db() as conn2:
        users = conn2.execute("SELECT username FROM users").fetchall()
        for u in users:
            conn2.execute("INSERT INTO notifications (username, message, date, read) VALUES (?, ?, ?, 0)",
                         (u['username'], message, now))
        conn2.commit()

def process_daily_earnings(username):
    """Advances each approved investment's day counter once full 24-hour periods have
    passed since the last update, so the card can show earnings accruing day by day.
    The wallet (bal_invest) is only credited once, in a single lump sum, when the plan
    fully matures — users cannot claim/withdraw any of it before then."""
    with get_db() as conn:
        invests = conn.execute("SELECT * FROM investments WHERE username=? AND status='approved'", (username,)).fetchall()
        now = datetime.now()
        
        for inv in invests:
            plan = get_plan_by_id(inv['plan_id'])
            if not plan: continue
            
            # Use 'date' column as the last paid timestamp
            inv_date_obj = datetime.strptime(inv['date'], "%Y-%m-%d %H:%M:%S")
            hours_diff = (now - inv_date_obj).total_seconds() / 3600
            days_diff = int(hours_diff // 24)
            
            if days_diff > 0 and inv['days_elapsed'] < plan['period']:
                days_to_add = min(days_diff, plan['period'] - inv['days_elapsed'])
                new_days_elapsed = inv['days_elapsed'] + days_to_add
                new_date = (inv_date_obj + timedelta(days=days_to_add)).strftime("%Y-%m-%d %H:%M:%S")
                
                conn.execute("UPDATE investments SET days_elapsed = days_elapsed + ?, date = ? WHERE id = ?", (days_to_add, new_date, inv['id']))
                
                # Only pay out once the plan is fully matured — this is the "claim" moment.
                if new_days_elapsed >= plan['period']:
                    conn.execute("UPDATE users SET bal_invest = bal_invest + ? WHERE username = ?", (plan['total'], username))
                    send_notification(username, f"Your {plan['name']} investment has matured after {plan['period']} days! ₦{plan['total']} is now available to withdraw.", conn=conn)
        conn.commit()

# ==========================================
# MASTER HTML TEMPLATE
# ==========================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NexFund - Grow Your Money with Smart Investment Plans</title>
    <meta name="description" content="NexFund is a trusted online investment platform offering daily returns, secure task earnings, and a rewarding affiliate program. Sign up and start earning today.">
    <meta name="keywords" content="NexFund, online investment, daily income, investment plans, affiliate earnings, task earnings, make money online, Nigeria investment platform">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="{{ request.url_root }}">
    <meta property="og:type" content="website">
    <meta property="og:title" content="NexFund - Grow Your Money with Smart Investment Plans">
    <meta property="og:description" content="Join NexFund for secure daily-return investment plans, task earnings, and a 5% referral bonus program.">
    <meta property="og:url" content="{{ request.url_root }}">
    <meta property="og:site_name" content="NexFund">
    <meta name="twitter:card" content="summary">
    <meta name="twitter:title" content="NexFund - Grow Your Money with Smart Investment Plans">
    <meta name="twitter:description" content="Join NexFund for secure daily-return investment plans, task earnings, and a 5% referral bonus program.">
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/icon?family=Material+Icons" rel="stylesheet">
    <style>
        body { font-family: 'Segoe UI', system-ui, sans-serif; background-color: #fdf8ff; }
        .md3-card { background: #ffffff; border-radius: 24px; box-shadow: 0 4px 20px rgba(103, 80, 164, 0.05); border: 1px solid #f3e8ff; }
        .hide-scrollbar::-webkit-scrollbar { display: none; }
        .admin-tab { animation: fadeIn 0.3s ease-in-out; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }
        /* SPA Loading Bar Animation */
        #spa-bar-container { pointer-events: none; position: fixed; z-index: 9999; top: 0; left: 0; width: 100%; height: 3px; }
        #spa-bar { background: #9333ea; width: 100%; height: 100%; transform: scaleX(0); transform-origin: left; transition: transform 0.3s ease; }
        @keyframes spin { 100% { transform: rotate(360deg); } }
        .animate-spin-fast { animation: spin 0.8s linear infinite; }
    </style>
</head>
<body class="text-gray-800">

    <div id="spa-bar-container"><div id="spa-bar"></div></div>

    <div id="app-root" class="min-h-screen flex flex-col relative pb-24 md:pb-10">
        
        <div id="toast-container" class="fixed top-4 left-0 w-full flex flex-col items-center z-[9999] px-4 pointer-events-none space-y-2">
            {% with messages = get_flashed_messages(with_categories=true) %}
              {% if messages %}
                {% for category, message in messages %}
                  <div class="toast-msg bg-gray-800 text-white px-6 py-3 rounded-full shadow-lg text-sm pointer-events-auto cursor-pointer transition-opacity duration-300" onclick="this.style.opacity='0'; setTimeout(()=>this.remove(),300)">
                    {{ message }}
                  </div>
                {% endfor %}
              {% endif %}
            {% endwith %}
        </div>

        <div id="fullscreen-viewer" class="hidden fixed inset-0 bg-black/95 z-[9999] flex items-center justify-center p-4 cursor-zoom-out opacity-0 transition-opacity duration-300" onclick="closeFullscreen()">
            <img id="fullscreen-img" src="" class="max-w-full max-h-full object-contain rounded-lg shadow-2xl scale-95 transition-transform duration-300">
            <button class="absolute top-6 right-6 text-white bg-white/20 p-2 rounded-full hover:bg-white/40 transition"><span class="material-icons">close</span></button>
        </div>

        

        {% if request.path == '/dashboard' %}
        <div id="telegram-popup" class="hidden fixed inset-0 bg-black/60 z-[9998] flex items-center justify-center p-4">
            <div class="bg-white rounded-[28px] w-full max-w-sm p-6 shadow-2xl text-center relative">
                <button type="button" onclick="dismissTelegramPopup()" class="absolute top-4 right-4 bg-gray-100 hover:bg-gray-200 text-gray-500 rounded-full p-1"><span class="material-icons text-lg">close</span></button>
                <div class="bg-purple-100 w-16 h-16 rounded-full flex items-center justify-center mx-auto mb-4 text-purple-600"><span class="material-icons text-3xl">send</span></div>
                <h3 class="text-xl font-bold text-purple-900 mb-2">Stay Updated!</h3>
                <p class="text-gray-500 text-sm mb-6">Join our official Telegram channel and discussion group for news, updates, and community support.</p>
                <a href="https://t.me/Nexfundd" target="_blank" data-no-spa class="block w-full bg-purple-600 text-white font-bold py-3 rounded-full mb-3 hover:bg-purple-700">Join Telegram Channel</a>
                <a href="https://t.me/+1uIRzgeDV_NjMDRk" target="_blank" data-no-spa class="block w-full bg-purple-100 text-purple-700 font-bold py-3 rounded-full mb-3 hover:bg-purple-200">Join Discussion Group</a>
                <button type="button" onclick="dismissTelegramPopup()" class="text-gray-400 text-sm font-medium mt-1">Maybe later</button>
            </div>
        </div>
        {% endif %}

        <div class="flex-1 w-full mx-auto {% if request.path != '/' %}max-w-5xl{% endif %}" id="main-content">
            {% block content %}{% endblock %}
        </div>

        {% if session.get('user') and session.get('user') != 'admin' %}
        <div class="fixed bottom-0 left-0 w-full bg-white border-t border-purple-100 flex justify-around items-center h-20 px-2 shadow-[0_-4px_20px_rgba(0,0,0,0.1)] z-40 md:hidden">
            <a href="/dashboard" class="flex flex-col items-center w-16 text-purple-600 {% if request.path == '/dashboard' %}font-bold text-purple-900{% endif %}">
                <div class="flex items-center justify-center w-16 h-8 rounded-full transition-colors {% if request.path == '/dashboard' %}bg-purple-100{% endif %}"><span class="material-icons">home</span></div>
                <span class="text-[11px] mt-1">Home</span>
            </a>
            <a href="/investments" class="flex flex-col items-center w-16 text-purple-600 {% if request.path == '/investments' %}font-bold text-purple-900{% endif %}">
                <div class="flex items-center justify-center w-16 h-8 rounded-full transition-colors {% if request.path == '/investments' %}bg-purple-100{% endif %}"><span class="material-icons">trending_up</span></div>
                <span class="text-[11px] mt-1">Plans</span>
            </a>
            <a href="/tasks" class="flex flex-col items-center w-16 text-purple-600 {% if request.path == '/tasks' %}font-bold text-purple-900{% endif %}">
                <div class="flex items-center justify-center w-16 h-8 rounded-full transition-colors {% if request.path == '/tasks' %}bg-purple-100{% endif %}"><span class="material-icons">task_alt</span></div>
                <span class="text-[11px] mt-1">Tasks</span>
            </a>
            <a href="/withdrawals" class="flex flex-col items-center w-16 text-purple-600 {% if request.path == '/withdrawals' %}font-bold text-purple-900{% endif %}">
                <div class="flex items-center justify-center w-16 h-8 rounded-full transition-colors {% if request.path == '/withdrawals' %}bg-purple-100{% endif %}"><span class="material-icons">account_balance_wallet</span></div>
                <span class="text-[11px] mt-1">Withdraw</span>
            </a>
        </div>
        {% endif %}

        {% if session.get('user') == 'admin' %}
        <div class="fixed bottom-0 left-0 w-full bg-white border-t border-purple-100 flex justify-around items-center h-20 px-2 shadow-[0_-4px_20px_rgba(0,0,0,0.1)] z-40 md:hidden">
            <button type="button" onclick="switchAdminTab('tab-dash')" class="admin-nav-btn text-purple-900 font-bold flex flex-col items-center w-16" id="nav-tab-dash">
                <div class="flex items-center justify-center w-16 h-8 rounded-full bg-purple-100 admin-nav-pill"><span class="material-icons">home</span></div>
                <span class="text-[11px] mt-1">Home</span>
            </button>
            <button type="button" onclick="switchAdminTab('tab-tasks')" class="admin-nav-btn text-purple-600 flex flex-col items-center w-16" id="nav-tab-tasks">
                <div class="flex items-center justify-center w-16 h-8 rounded-full admin-nav-pill"><span class="material-icons">task</span></div>
                <span class="text-[11px] mt-1">Tasks</span>
            </button>
            <button type="button" onclick="switchAdminTab('tab-invests')" class="admin-nav-btn text-purple-600 flex flex-col items-center w-16" id="nav-tab-invests">
                <div class="flex items-center justify-center w-16 h-8 rounded-full admin-nav-pill"><span class="material-icons">trending_up</span></div>
                <span class="text-[11px] mt-1">Invests</span>
            </button>
            <button type="button" onclick="switchAdminTab('tab-withdraws')" class="admin-nav-btn text-purple-600 flex flex-col items-center w-16" id="nav-tab-withdraws">
                <div class="flex items-center justify-center w-16 h-8 rounded-full admin-nav-pill"><span class="material-icons">account_balance_wallet</span></div>
                <span class="text-[11px] mt-1">Withdraws</span>
            </button>
            <button type="button" onclick="switchAdminTab('tab-users')" class="admin-nav-btn text-purple-600 flex flex-col items-center w-16" id="nav-tab-users">
                <div class="flex items-center justify-center w-16 h-8 rounded-full admin-nav-pill"><span class="material-icons">manage_accounts</span></div>
                <span class="text-[11px] mt-1">Users</span>
            </button>
        </div>
        {% endif %}
    </div>

    <script>
        const spaBar = document.getElementById('spa-bar');

        function initToasts() {
            document.querySelectorAll('.toast-msg').forEach(toast => {
                setTimeout(() => {
                    toast.style.opacity = '0';
                    setTimeout(() => toast.remove(), 300);
                }, 3000);
            });
        }
        initToasts();

        function showJSToast(msg) {
            const container = document.getElementById('toast-container');
            const toast = document.createElement('div');
            toast.className = 'bg-gray-800 text-white px-6 py-3 rounded-full shadow-lg text-sm pointer-events-auto cursor-pointer transition-opacity duration-300';
            toast.innerText = msg;
            toast.onclick = () => toast.remove();
            container.appendChild(toast);
            setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 300); }, 3000);
        }

        function openFullscreen(src) {
            const viewer = document.getElementById('fullscreen-viewer');
            const img = document.getElementById('fullscreen-img');
            img.src = src;
            viewer.classList.remove('hidden');
            setTimeout(() => {
                viewer.classList.remove('opacity-0');
                img.classList.remove('scale-95');
                img.classList.add('scale-100');
            }, 10);
        }

        function closeFullscreen() {
            const viewer = document.getElementById('fullscreen-viewer');
            const img = document.getElementById('fullscreen-img');
            viewer.classList.add('opacity-0');
            img.classList.remove('scale-100');
            img.classList.add('scale-95');
            setTimeout(() => {
                viewer.classList.add('hidden');
                img.src = '';
            }, 300);
        }

        async function navigateSPA(url, options = {}, push = true) {
            spaBar.style.transform = 'scaleX(0.3)';
            try {
                const res = await fetch(url, options);
                spaBar.style.transform = 'scaleX(0.7)';
                const html = await res.text();
                
                const parser = new DOMParser();
                const doc = parser.parseFromString(html, 'text/html');
                
                let activeAdminTab = null;
                const activeTabEl = document.querySelector('.admin-tab:not(.hidden)');
                if (activeTabEl) activeAdminTab = activeTabEl.id;

                const newRoot = doc.getElementById('app-root');
                if (newRoot) {
                    document.getElementById('app-root').innerHTML = newRoot.innerHTML;
                    
                    const finalUrl = res.url || url;
                    if (push && finalUrl !== window.location.href) {
                        history.pushState({}, '', finalUrl);
                    }
                    
                    if (activeAdminTab && typeof switchAdminTab === 'function' && document.getElementById(activeAdminTab)) {
                        switchAdminTab(activeAdminTab);
                    } else if (!options.method || options.method === 'GET') {
                        window.scrollTo({ top: 0, behavior: 'smooth' });
                    }
                    
                    initToasts();
                    checkTelegramPopup();
                }
            } catch (err) {
                console.error('SPA Navigation Error:', err);
                window.location.href = url;
            } finally {
                spaBar.style.transform = 'scaleX(1)';
                setTimeout(() => spaBar.style.transform = 'scaleX(0)', 300);
            }
        }

        document.addEventListener('click', (e) => {
            const link = e.target.closest('a');
            if (link && link.href && link.href.startsWith(window.location.origin) && link.target !== '_blank' && !link.hasAttribute('data-no-spa')) {
                e.preventDefault();
                navigateSPA(link.href);
            }
        });

        document.addEventListener('submit', (e) => {
            const form = e.target;
            if (form.hasAttribute('data-no-spa')) return;
            e.preventDefault();
            const btn = form.querySelector('button[type="submit"]');
            const originalText = btn ? btn.innerHTML : '';
            
            if (btn) {
                btn.disabled = true;
                btn.style.opacity = '0.7';
                btn.innerHTML = '<span class="material-icons animate-spin-fast text-sm align-middle">sync</span>';
            }

            const formData = new FormData(form);
            
            let url = window.location.href;
            const attrAction = form.getAttribute('action');
            if (attrAction && typeof attrAction === 'string') {
                url = attrAction;
            }
            
            let method = 'POST';
            const attrMethod = form.getAttribute('method');
            if (attrMethod && typeof attrMethod === 'string') {
                method = attrMethod.toUpperCase();
            }

            let fetchOptions = { method: method };
            if (method === 'GET') {
                const params = new URLSearchParams(formData).toString();
                const [base] = url.split('?');
                url = params ? `${base}?${params}` : base;
            } else {
                fetchOptions.body = formData;
            }

            navigateSPA(url, fetchOptions).finally(() => {
                if (document.body.contains(form) && btn) {
                    btn.disabled = false;
                    btn.style.opacity = '1';
                    btn.innerHTML = originalText;
                }
            });
        });

        window.addEventListener('popstate', () => {
            navigateSPA(window.location.href, {}, false);
        });

        function dismissTelegramPopup() {
            const popup = document.getElementById('telegram-popup');
            if (popup) popup.classList.add('hidden');
            sessionStorage.setItem('nf_telegram_dismissed', '1');
        }

        function checkTelegramPopup() {
            const popup = document.getElementById('telegram-popup');
            if (popup && window.location.pathname === '/dashboard' && !sessionStorage.getItem('nf_telegram_dismissed')) {
                setTimeout(() => popup.classList.remove('hidden'), 600);
            }
        }

        checkTelegramPopup();

        function compressAndPreview(input) {
            const file = input.files[0];
            if (!file) return;
            const label = input.previousElementSibling;
            if (label) {
                label.innerHTML = '<span class="material-icons text-purple-400 mb-2 text-3xl animate-pulse">hourglass_top</span><p class="text-xs text-gray-500">Compressing image...</p>';
            }
            const reader = new FileReader();
            reader.onload = function(e) {
                const img = new Image();
                img.onload = function() {
                    const MAX_DIM = 1600;
                    let w = img.width, h = img.height;
                    if (w > MAX_DIM || h > MAX_DIM) {
                        const scale = MAX_DIM / Math.max(w, h);
                        w = Math.round(w * scale);
                        h = Math.round(h * scale);
                    }
                    const canvas = document.createElement('canvas');
                    canvas.width = w;
                    canvas.height = h;
                    canvas.getContext('2d').drawImage(img, 0, 0, w, h);
                    canvas.toBlob(function(blob) {
                        const newName = file.name.replace(/\.[^/.]+$/, '') + '.jpg';
                        const compressedFile = new File([blob], newName, { type: 'image/jpeg' });
                        const dt = new DataTransfer();
                        dt.items.add(compressedFile);
                        input.files = dt.files;
                        if (label) {
                            label.innerHTML = '<span class="material-icons text-green-500 mb-2 text-3xl">check_circle</span><p class="text-sm font-bold text-purple-900 truncate w-full">' + newName + '</p><p class="text-xs text-green-600 font-bold">Ready to submit (' + Math.round(blob.size/1024) + ' KB)</p>';
                        }
                    }, 'image/jpeg', 0.9);
                };
                img.onerror = function() {
                    if (label) {
                        label.innerHTML = '<span class="material-icons text-green-500 mb-2 text-3xl">check_circle</span><p class="text-sm font-bold text-purple-900 truncate w-full">' + file.name + '</p><p class="text-xs text-green-600 font-bold">Ready to submit</p>';
                    }
                };
                img.src = e.target.result;
            };
            reader.readAsDataURL(file);
        }

        function togglePassword() {
            var x = document.getElementById("password");
            var icon = document.getElementById("toggleIcon");
            if (x) {
                if (x.type === "password") { x.type = "text"; icon.innerHTML = "visibility_off"; } 
                else { x.type = "password"; icon.innerHTML = "visibility"; }
            }
        }

        function switchAdminTab(tabId) {
            document.querySelectorAll('.admin-tab').forEach(el => el.classList.add('hidden'));
            const target = document.getElementById(tabId);
            if(target) target.classList.remove('hidden');
            
            document.querySelectorAll('.admin-nav-btn').forEach(btn => {
                btn.classList.remove('text-purple-900', 'font-bold');
                btn.classList.add('text-purple-600');
                const pill = btn.querySelector('.admin-nav-pill');
                if(pill) pill.classList.remove('bg-purple-100');
            });
            
            const activeBtn = document.getElementById('nav-' + tabId);
            if(activeBtn) {
                activeBtn.classList.remove('text-purple-600');
                activeBtn.classList.add('text-purple-900', 'font-bold');
                const pill = activeBtn.querySelector('.admin-nav-pill');
                if(pill) pill.classList.add('bg-purple-100');
            }
        }

        function switchInvestTab(tab) {
            document.querySelectorAll('.invest-tab').forEach(el => el.classList.add('hidden'));
            const target = document.getElementById('tab-' + tab);
            if (target) target.classList.remove('hidden');

            document.querySelectorAll('#nav-tab-10day, #nav-tab-monthly').forEach(btn => {
                btn.classList.remove('bg-white', 'text-purple-900', 'shadow');
                btn.classList.add('text-gray-500');
            });
            const activeBtn = document.getElementById('nav-tab-' + tab);
            if (activeBtn) {
                activeBtn.classList.remove('text-gray-500');
                activeBtn.classList.add('bg-white', 'text-purple-900', 'shadow');
            }
        }
    </script>
</body>
</html>
"""

# ==========================================
# ROUTES
# ==========================================
@app.route('/')
def landing():
    if 'user' in session:
        return redirect(url_for('admin_dashboard' if session['user'] == 'admin' else 'dashboard'))
    
    content = """
    {% block content %}
    <div class="flex justify-between items-center px-6 md:px-12 py-6 bg-purple-700 text-white">
        <h1 class="text-2xl font-bold tracking-wider">NexFund</h1>
        <div class="flex items-center gap-4">
            <a href="/auth?mode=login" class="font-medium hover:text-purple-200 transition-colors">Login</a>
            <a href="/auth?mode=register" class="bg-white text-purple-700 px-5 py-2 rounded-full font-bold text-sm hover:bg-purple-50 transition-colors shadow-sm">Sign Up</a>
        </div>
    </div>
    
    <div class="bg-purple-700 text-white px-8 pb-16 pt-8 md:p-16 text-center rounded-b-[40px] shadow-lg">
        <h1 class="text-5xl md:text-7xl font-extrabold mb-4 tracking-tight">NexFund</h1>
        <p class="text-lg md:text-xl text-purple-100 mb-8 max-w-lg mx-auto">The ultimate task site. Registration is 100% Free! Earn daily, complete simple tasks, and invite friends.</p>
        <a href="/auth?mode=register" class="inline-block bg-white text-purple-700 px-8 py-4 rounded-full font-bold text-lg shadow-md hover:bg-purple-50 transition-all">Get Started Now</a>
    </div>
    <div class="grid grid-cols-1 md:grid-cols-3 gap-6 p-6 mt-6 max-w-5xl mx-auto">
        <div class="md3-card p-6 text-center">
            <div class="bg-purple-100 w-16 h-16 rounded-full flex items-center justify-center mx-auto mb-4 text-purple-600"><span class="material-icons text-3xl">payments</span></div>
            <h3 class="text-xl font-bold text-purple-900 mb-2">High Returns</h3>
            <p class="text-gray-600">Daily earnings ranging from ₦500 to ₦5,000 via our secure plans.</p>
        </div>
        <div class="md3-card p-6 text-center">
            <div class="bg-purple-100 w-16 h-16 rounded-full flex items-center justify-center mx-auto mb-4 text-purple-600"><span class="material-icons text-3xl">people</span></div>
            <h3 class="text-xl font-bold text-purple-900 mb-2">Referral Bonus</h3>
            <p class="text-gray-600">Earn a solid ₦50 bonus for every active referral you bring in.</p>
        </div>
        <div class="md3-card p-6 text-center">
            <div class="bg-purple-100 w-16 h-16 rounded-full flex items-center justify-center mx-auto mb-4 text-purple-600"><span class="material-icons text-3xl">thumb_up</span></div>
            <h3 class="text-xl font-bold text-purple-900 mb-2">Social Tasks</h3>
            <p class="text-gray-600">Get paid instantly for completing easy social media tasks.</p>
        </div>
    </div>
    <div class="text-center mt-4 mb-16"><a href="/auth?mode=login" class="text-purple-700 font-bold underline">Already have an account? Login here</a></div>

    <footer class="mt-12 bg-purple-50 p-6 text-center text-sm text-purple-900 border-t border-purple-100">
      <p class="font-bold mb-2 text-base">Website Designed by Tenoco Services</p>
      <a href="https://wa.me/9096831191?text=Hi%20Tenoco%20Services,%20I%20want%20you%20to%20build%20a%20site%20for%20me%20just%20like%20NexFund!" 
         target="_blank" class="inline-block bg-purple-200 text-purple-800 px-5 py-2 rounded-full font-bold hover:bg-purple-300 transition-colors mb-4" data-no-spa>
        Chat with Tenoco
      </a>
      <p class="text-xs text-purple-600/80 leading-relaxed italic max-w-lg mx-auto">
        Disclaimer: Tenoco Services does not own or control this site at all and is not responsible for how the site is used. We just built it and handed over the keys—so play nice! 🚀
      </p>
    </footer>
    {% endblock %}
    """
    return render_page(content)

@app.route('/auth', methods=['GET', 'POST'])
def auth():
    mode = request.args.get('mode', 'login')
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        req_action = request.form.get('req_action')

        if req_action == 'login':
            if username == ADMIN_CREDS['username'] and password == ADMIN_CREDS['password']:
                session.permanent = True
                session['user'] = 'admin'
                return redirect(url_for('admin_dashboard'))
            
            with get_db() as conn:
                user = conn.execute("SELECT * FROM users WHERE username=? AND password=?", (username, password)).fetchone()
                if user and user['banned']:
                    flash("Your account has been suspended. Contact support.", "error")
                elif user:
                    session.permanent = True
                    session['user'] = username
                    flash("Login successful!", "success")
                    return redirect(url_for('dashboard'))
                else:
                    flash("Invalid credentials", "error")

        elif req_action == 'register':
            ref_code = request.form.get('ref_code', '')
            captcha_input = request.form.get('captcha', '').strip()

            if captcha_input.upper() != session.get('captcha', ''):
                flash("Incorrect Captcha. Please try again.", "error")
            else:
                with get_db() as conn:
                    existing = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
                    if existing or username == ADMIN_CREDS['username']:
                        flash("Username already exists", "error")
                    else:
                        conn.execute("""INSERT INTO users (username, password, referred_by, bal_affiliate, bal_invest, bal_task, bank_name, acc_no, acc_name, ref_tasks_count) 
                                        VALUES (?, ?, ?, 0, 0, 0, '', '', '', 0)""", 
                                     (username, password, ref_code))
                        if ref_code:
                            conn.execute("UPDATE users SET bal_affiliate = bal_affiliate + 50 WHERE username=?", (ref_code,))
                        conn.commit()
                        session.pop('captcha', None)
                        session.permanent = True
                        session['user'] = username
                        flash("Registration successful!", "success")
                        return redirect(url_for('dashboard'))

    session['captcha'] = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

    content = """
    {% block content %}
    <div class="min-h-screen flex flex-col justify-center items-center p-4">
        <div class="w-full max-w-md bg-white rounded-[32px] p-8 shadow-xl border border-purple-50">
            <h2 class="text-3xl font-bold text-purple-900 text-center mb-6">{{ 'Create Account' if request.args.get('mode') == 'register' else 'Welcome Back' }}</h2>
            <form method="POST" class="space-y-5">
                <input type="hidden" name="req_action" value="{{ request.args.get('mode', 'login') }}">
                <div>
                    <label class="block text-sm font-medium text-purple-800 mb-1 ml-2">Username</label>
                    <input type="text" name="username" required class="w-full bg-purple-50 border border-purple-100 rounded-full px-5 py-4 focus:outline-none focus:ring-2 focus:ring-purple-400">
                </div>
                <div class="relative">
                    <label class="block text-sm font-medium text-purple-800 mb-1 ml-2">Password</label>
                    <input type="password" name="password" id="password" required class="w-full bg-purple-50 border border-purple-100 rounded-full px-5 py-4 focus:outline-none focus:ring-2 focus:ring-purple-400">
                    <button type="button" onclick="togglePassword()" class="absolute right-4 top-10 text-purple-500"><span class="material-icons" id="toggleIcon">visibility</span></button>
                </div>
                {% if request.args.get('mode') == 'register' %}
                <div>
                    <label class="block text-sm font-medium text-purple-800 mb-1 ml-2">Referral Code (Optional)</label>
                    <input type="text" name="ref_code" class="w-full bg-purple-50 border border-purple-100 rounded-full px-5 py-4 focus:outline-none focus:ring-2 focus:ring-purple-400" placeholder="Who invited you?" value="{{ request.args.get('ref_code', '') }}">
                </div>
                <div>
                    <label class="block text-sm font-medium text-purple-800 mb-1 ml-2">Enter the code below</label>
                    <div class="flex items-center gap-3">
                        <div class="bg-purple-900 text-white font-mono font-bold tracking-[0.3em] text-xl px-5 py-3 rounded-2xl select-none" style="letter-spacing:0.3em;">{{ session.get('captcha','') }}</div>
                        <a href="/auth?mode=register" data-no-spa class="text-purple-600 flex items-center justify-center bg-purple-50 hover:bg-purple-100 rounded-full p-3"><span class="material-icons">refresh</span></a>
                    </div>
                    <input type="text" name="captcha" required autocomplete="off" placeholder="Type the code shown above" class="w-full mt-2 bg-purple-50 border border-purple-100 rounded-full px-5 py-4 focus:outline-none focus:ring-2 focus:ring-purple-400">
                </div>
                {% endif %}
                <button type="submit" class="w-full bg-purple-600 text-white rounded-full py-4 font-bold text-lg hover:bg-purple-700 shadow-md">{{ 'Register' if request.args.get('mode') == 'register' else 'Login' }}</button>
            </form>
            <div class="mt-6 text-center">
                {% if request.args.get('mode') == 'register' %}
                <a href="/auth?mode=login" class="text-purple-600 font-medium hover:underline">Already have an account? Log in</a>
                {% else %}
                <a href="/auth?mode=register" class="text-purple-600 font-medium hover:underline">Don't have an account? Sign up</a>
                {% endif %}
            </div>
        </div>
        <a href="/" class="mt-8 text-gray-500 font-medium hover:text-purple-700">← Back to Home</a>
    </div>
    {% endblock %}
    """
    return render_page(content)



@app.route('/dashboard')
def dashboard():
    username, user = get_user()
    if not user: return redirect(url_for('landing'))
    
    process_daily_earnings(username)
    _, user = get_user() # Refetch fresh data after earnings calculation
    
    total = user['bal_affiliate'] + user['bal_invest'] + user['bal_task']
    ref_link = f"https://nexfund-ng.vercel.app/auth?mode=register&ref_code={username}"
    with get_db() as conn:
        ref_count = conn.execute("SELECT COUNT(*) FROM users WHERE referred_by=?", (username,)).fetchone()[0]

    content = """
    {% block content %}
    <div class="hidden md:flex bg-purple-700 text-white px-8 py-4 justify-between items-center shadow-md mb-6 rounded-b-[24px]">
        <h1 class="text-2xl font-bold">NexFund</h1>
        <div class="flex space-x-6 font-medium">
            <a href="/dashboard" class="text-white border-b-2 border-white pb-1">Dashboard</a>
            <a href="/investments" class="text-purple-200 hover:text-white pb-1">Plans</a>
            <a href="/tasks" class="text-purple-200 hover:text-white pb-1">Tasks</a>
            <a href="/withdrawals" class="text-purple-200 hover:text-white pb-1">Withdraw</a>
            <a href="/notifications" class="text-purple-200 hover:text-white relative flex items-center" data-no-spa>
                <span class="material-icons text-sm mr-1">notifications</span> Notifications
                {% if unread_notifications and unread_notifications > 0 %}
                <span class="absolute -top-2 -right-2 bg-red-500 text-white text-[10px] font-bold rounded-full w-5 h-5 flex items-center justify-center">{{ unread_notifications }}</span>
                {% endif %}
            </a>
            <a href="/logout" data-no-spa class="text-purple-200 hover:text-white flex items-center"><span class="material-icons text-sm mr-1">logout</span> Logout</a>
        </div>
    </div>
    
    <div class="p-4 md:px-8">
        <div class="flex justify-between items-center mb-6 md:hidden">
            <h2 class="text-2xl font-bold text-purple-900">Dashboard</h2>
            <div class="flex items-center gap-2">
                <a href="/notifications" class="relative text-purple-500 bg-purple-100 p-2 rounded-full hover:bg-purple-200 transition-colors" data-no-spa>
                    <span class="material-icons">notifications</span>
                    {% if unread_notifications and unread_notifications > 0 %}
                    <span class="absolute -top-1 -right-1 bg-red-500 text-white text-[10px] font-bold rounded-full w-5 h-5 flex items-center justify-center">{{ unread_notifications }}</span>
                    {% endif %}
                </a>
                <a href="/logout" data-no-spa class="text-purple-500 bg-purple-100 p-2 rounded-full hover:bg-purple-200 transition-colors"><span class="material-icons">logout</span></a>
            </div>
        </div>
        <div class="bg-purple-600 text-white rounded-[32px] p-8 shadow-lg mb-6 relative overflow-hidden">
            <p class="text-purple-100 text-sm font-medium mb-1">Total Balance</p>
            <h1 class="text-4xl font-extrabold mb-6">₦{{ total }}</h1>
            <div class="grid grid-cols-3 gap-2 border-t border-purple-500 pt-4">
                <div><p class="text-xs text-purple-200">Affiliate</p><p class="font-bold text-lg">₦{{ user.bal_affiliate }}</p></div>
                <div><p class="text-xs text-purple-200">Investment</p><p class="font-bold text-lg">₦{{ user.bal_invest }}</p></div>
                <div><p class="text-xs text-purple-200">Tasks</p><p class="font-bold text-lg">₦{{ user.bal_task }}</p></div>
            </div>
        </div>
        <div class="md3-card p-6">
            <p class="text-sm font-medium text-gray-500 mb-2">Your Referral Link</p>
            <div class="flex items-center bg-gray-50 rounded-xl border border-gray-200 p-2">
                <input type="text" readonly value="{{ ref_link }}" class="flex-1 bg-transparent text-sm text-gray-700 outline-none px-2" id="refInput">
                <button type="button" onclick="navigator.clipboard.writeText(document.getElementById('refInput').value); showJSToast('Link Copied!');" class="bg-purple-100 text-purple-600 p-2 rounded-lg"><span class="material-icons">content_copy</span></button>
            </div>
            <p class="text-xs text-gray-400 mt-2">Earn ₦50 per referral automatically!</p>
            <p class="text-xs text-purple-700 font-bold mt-2 flex items-center gap-1"><span class="material-icons text-sm">group</span> Total Referrals: {{ ref_count }}</p>
        </div>
    </div>
    {% endblock %}
    """
    return render_page(content, user=user, total=total, ref_link=ref_link, ref_count=ref_count)

@app.route('/investments', methods=['GET', 'POST'])
def investments():
    username, user = get_user()
    if not user: return redirect(url_for('landing'))

    if request.method == 'POST':
        plan_id = int(request.form['plan_id'])
        sender_name = request.form.get('sender_name', '').strip()
        proof = process_file(request.files.get('proof'), subfolder='investments')
        if not sender_name:
            flash("Please enter the full sender name used for the transfer.", "error")
        elif proof:
            with get_db() as conn:
                conn.execute("INSERT INTO investments (username, plan_id, proof_b64, status, date, days_elapsed, sender_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (username, plan_id, proof, 'pending', datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 0, sender_name))
                conn.commit()
            flash("Payment submitted for admin approval! (6-24 hrs)", "success")
        else:
            flash("Screenshot proof required.", "error")
        return redirect(url_for('investments'))

    process_daily_earnings(username)

    with get_db() as conn:
        my_invests_raw = conn.execute("SELECT * FROM investments WHERE username=? ORDER BY id DESC", (username,)).fetchall()

    my_invests = []
    for inv in my_invests_raw:
        row = dict(inv)
        plan = get_plan_by_id(row['plan_id'])
        if not plan:
            continue
        row['plan'] = plan
        row['matured'] = row['days_elapsed'] >= plan['period']
        row['accrued'] = row['days_elapsed'] * plan['daily']
        my_invests.append(row)

    my_invests_10day = [i for i in my_invests if i['plan']['tab'] == '10day']
    my_invests_monthly = [i for i in my_invests if i['plan']['tab'] == 'monthly']
    
    content = """
    {% block content %}
    <div class="hidden md:flex bg-purple-700 text-white px-8 py-4 justify-between items-center shadow-md mb-6 rounded-b-[24px]">
        <h1 class="text-2xl font-bold">NexFund</h1>
        <div class="flex space-x-6 font-medium">
            <a href="/dashboard" class="text-purple-200 hover:text-white pb-1">Dashboard</a>
            <a href="/investments" class="text-white border-b-2 border-white pb-1">Plans</a>
            <a href="/tasks" class="text-purple-200 hover:text-white pb-1">Tasks</a>
            <a href="/withdrawals" class="text-purple-200 hover:text-white pb-1">Withdraw</a>
            <a href="/logout" data-no-spa class="text-purple-200 hover:text-white flex items-center"><span class="material-icons text-sm mr-1">logout</span> Logout</a>
        </div>
    </div>
    
    <div class="p-4 md:px-8">
        <div class="flex justify-between items-center mb-4">
            <h2 class="text-2xl font-bold text-purple-900">Investment Plans</h2>
        </div>

        <div class="flex gap-2 bg-gray-100 p-1 rounded-full mb-6 max-w-xs">
            <button type="button" id="nav-tab-10day" onclick="switchInvestTab('10day')" class="flex-1 py-2 rounded-full text-sm font-bold bg-white text-purple-900 shadow">10 Days</button>
            <button type="button" id="nav-tab-monthly" onclick="switchInvestTab('monthly')" class="flex-1 py-2 rounded-full text-sm font-bold text-gray-500">Monthly</button>
        </div>

        {% macro invest_card(inv) %}
        <div class="md3-card p-4 flex justify-between items-center">
            <div>
                <p class="font-bold text-purple-900">{{ inv.plan.name }}</p>
                <p class="text-xs text-gray-500">Accrued: ₦{{ inv.accrued }} (Day {{ inv.days_elapsed }}/{{ inv.plan.period }})</p>
                {% if inv.status=='approved' %}
                    {% if inv.matured %}
                    <p class="text-xs font-bold text-green-600 flex items-center gap-1 mt-1"><span class="material-icons text-sm">lock_open</span> Available to withdraw</p>
                    {% else %}
                    <p class="text-xs font-bold text-orange-500 flex items-center gap-1 mt-1"><span class="material-icons text-sm">lock</span> Locked until Day {{ inv.plan.period }}</p>
                    {% endif %}
                {% endif %}
            </div>
            <span class="px-3 py-1 rounded-full text-xs font-bold {% if inv.status=='approved' and inv.matured %}bg-gray-200 text-gray-600{% elif inv.status=='approved' %}bg-green-100 text-green-700{% elif inv.status=='rejected' %}bg-red-100 text-red-700{% else %}bg-orange-100 text-orange-700{% endif %}">
                {% if inv.status=='approved' and inv.matured %}EXPIRED{% elif inv.status=='approved' %}ACTIVE{% else %}{{ inv.status | upper }}{% endif %}
            </span>
        </div>
        {% endmacro %}

        {% macro plan_grid(plan_list) %}
        <div class="grid gap-4 md:grid-cols-2">
            {% for p in plan_list %}
            <div class="md3-card p-6 border-purple-100">
                <h3 class="text-xl font-bold text-purple-900 mb-2">{{ p.name }}</h3>
                <div class="space-y-2 mb-6">
                    <p class="flex justify-between"><span class="text-gray-400">Price:</span> <span class="font-bold">₦{{ p.price }}</span></p>
                    <p class="flex justify-between"><span class="text-gray-400">Daily Income:</span> <span class="font-bold text-green-600">₦{{ p.daily }}</span></p>
                    <p class="flex justify-between"><span class="text-gray-400">Period:</span> <span>{{ p.period }} Days</span></p>
                    <div class="h-px w-full bg-gray-100 my-2"></div>
                    <p class="flex justify-between text-purple-900 font-bold"><span>Total Return:</span> ₦{{ p.total }}</p>
                </div>
                <button type="button" onclick="document.getElementById('modal-{{ p.id }}').classList.remove('hidden')" class="w-full bg-purple-100 text-purple-700 font-bold py-3 rounded-full hover:bg-purple-600 hover:text-white transition-colors">Purchase Plan</button>
            </div>

            <div id="modal-{{ p.id }}" class="hidden fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4 backdrop-blur-sm transition-opacity">
                <div class="bg-white rounded-[32px] w-full max-w-md p-6 max-h-[90vh] overflow-y-auto shadow-2xl relative">
                    
                    <div class="flex justify-between items-center mb-6">
                        <div class="flex items-center gap-2">
                            <div class="bg-green-100 p-2 rounded-full text-green-600 flex items-center justify-center">
                                <span class="material-icons text-xl">verified_user</span>
                            </div>
                            <h3 class="text-xl font-bold text-gray-900">Secure Payment</h3>
                        </div>
                        <button type="button" onclick="document.getElementById('modal-{{ p.id }}').classList.add('hidden')" class="bg-gray-100 hover:bg-gray-200 text-gray-600 rounded-full p-2 transition-colors"><span class="material-icons text-lg">close</span></button>
                    </div>

                    <div class="text-center mb-6">
                        <p class="text-sm text-gray-500 font-medium mb-1">Amount to Pay ({{ p.name }})</p>
                        <h2 class="text-4xl font-extrabold text-purple-700">₦{{ p.price }}</h2>
                    </div>

                    <div class="bg-gradient-to-br from-gray-50 to-gray-100 rounded-2xl p-5 border border-gray-200 mb-6 shadow-inner relative overflow-hidden">
                        <div class="absolute top-0 right-0 w-24 h-24 bg-purple-500 opacity-5 rounded-bl-full"></div>
                        
                        <div class="mb-4 relative z-10">
                            <p class="text-[10px] text-gray-400 uppercase tracking-widest font-bold mb-1">Bank Name</p>
                            <p class="text-gray-900 font-bold text-sm">{{ account_details.bank }}</p>
                        </div>
                        
                        <div class="mb-4 relative z-10">
                            <p class="text-[10px] text-gray-400 uppercase tracking-widest font-bold mb-1">Account Name</p>
                            <p class="text-gray-900 font-bold uppercase text-sm">{{ account_details.name }}</p>
                        </div>
                        
                        <div class="relative z-10">
                            <p class="text-[10px] text-gray-400 uppercase tracking-widest font-bold mb-1">Account Number</p>
                            <div class="flex items-center justify-between bg-white px-3 py-2 rounded-xl border border-gray-200 shadow-sm">
                                <span class="text-xl font-mono font-bold text-purple-900 tracking-widest">{{ account_details.number }}</span>
                                <button type="button" onclick="navigator.clipboard.writeText('{{ account_details.number }}'); showJSToast('Account number copied!');" class="text-purple-600 bg-purple-50 hover:bg-purple-100 p-2 rounded-lg transition-colors flex items-center gap-1">
                                    <span class="material-icons text-sm">content_copy</span>
                                </button>
                            </div>
                        </div>
                    </div>

                    <form method="POST" enctype="multipart/form-data" class="space-y-4">
                        <input type="hidden" name="plan_id" value="{{ p.id }}">
                        <div>
                            <label class="block text-sm font-bold text-gray-700 mb-2">Sender's Full Name</label>
                            <input type="text" name="sender_name" required placeholder="Name used for the bank transfer" class="w-full bg-gray-50 border p-3 rounded-xl outline-none focus:ring-2 focus:ring-purple-200">
                        </div>
                        <div>
                            <p class="text-sm font-bold text-gray-700 mb-2">Upload Payment Proof</p>
                            <label class="flex flex-col items-center justify-center w-full h-32 border-2 border-dashed border-purple-300 rounded-2xl cursor-pointer bg-purple-50 hover:bg-purple-100 transition-colors text-center px-4">
                                <div class="file-display flex flex-col items-center justify-center pointer-events-none">
                                    <span class="material-icons text-purple-400 mb-2 text-3xl">cloud_upload</span>
                                    <p class="mb-1 text-sm text-gray-500"><span class="font-bold text-purple-600">Click to upload</span> screenshot</p>
                                    <p class="text-xs text-gray-400">PNG, JPG or JPEG</p>
                                </div>
                                <input type="file" name="proof" required accept="image/*" class="hidden" onchange="compressAndPreview(this)">
                            </label>
                        </div>
                        <button type="submit" class="w-full bg-purple-600 hover:bg-purple-700 text-white font-bold py-4 rounded-xl shadow-lg shadow-purple-200 transition-all flex justify-center items-center gap-2 mt-2">
                            <span class="material-icons text-lg">send</span> Submit for Approval
                        </button>
                    </form>
                </div>
            </div>
            {% endfor %}
        </div>
        {% endmacro %}

        <div id="tab-10day" class="invest-tab block">
            {% if my_invests_10day %}
            <div class="mb-8">
                <h3 class="text-lg font-bold text-gray-700 mb-3">Your Plans</h3>
                <div class="grid gap-4">
                    {% for inv in my_invests_10day %}{{ invest_card(inv) }}{% endfor %}
                </div>
            </div>
            {% endif %}
            {{ plan_grid(plans_10day) }}
        </div>

        <div id="tab-monthly" class="invest-tab hidden">
            {% if my_invests_monthly %}
            <div class="mb-8">
                <h3 class="text-lg font-bold text-gray-700 mb-3">Your Plans</h3>
                <div class="grid gap-4">
                    {% for inv in my_invests_monthly %}{{ invest_card(inv) }}{% endfor %}
                </div>
            </div>
            {% endif %}
            {{ plan_grid(plans_monthly) }}
        </div>
    </div>
    {% endblock %}
    """
    return render_page(content, plans=PLANS, plans_10day=PLANS_10DAY, plans_monthly=PLANS_MONTHLY, my_invests=my_invests, my_invests_10day=my_invests_10day, my_invests_monthly=my_invests_monthly, account_details=COMPANY_ACCOUNT)

@app.route('/tasks', methods=['GET', 'POST'])
def tasks():
    username, user = get_user()
    if not user: return redirect(url_for('landing'))

    if request.method == 'POST':
        task_id = int(request.form['task_id'])
        proof = process_file(request.files.get('proof'), subfolder='tasks')
        if proof:
            with get_db() as conn:
                conn.execute("DELETE FROM user_tasks WHERE username=? AND task_id=? AND status='rejected'", (username, task_id))
                conn.execute("INSERT INTO user_tasks (username, task_id, proof_b64, status, date) VALUES (?, ?, ?, ?, ?)",
                             (username, task_id, proof, 'pending', datetime.now().strftime("%Y-%m-%d")))
                conn.commit()
            flash("Task proof submitted!", "success")
        return redirect(url_for('tasks'))

    with get_db() as conn:
        user_tasks = conn.execute("SELECT * FROM user_tasks WHERE username=?", (username,)).fetchall()
        user_subs = {t['task_id']: t['status'] for t in user_tasks}
        approved_ids = {tid for tid, st in user_subs.items() if st == 'approved'}
        all_tasks = [t for t in conn.execute("SELECT * FROM tasks").fetchall() if t['id'] not in approved_ids]

    content = """
    {% block content %}
    <div class="hidden md:flex bg-purple-700 text-white px-8 py-4 justify-between items-center shadow-md mb-6 rounded-b-[24px]">
        <h1 class="text-2xl font-bold">NexFund</h1>
        <div class="flex space-x-6 font-medium">
            <a href="/dashboard" class="text-purple-200 hover:text-white pb-1">Dashboard</a>
            <a href="/investments" class="text-purple-200 hover:text-white pb-1">Plans</a>
            <a href="/tasks" class="text-white border-b-2 border-white pb-1">Tasks</a>
            <a href="/withdrawals" class="text-purple-200 hover:text-white pb-1">Withdraw</a>
            <a href="/logout" data-no-spa class="text-purple-200 hover:text-white flex items-center"><span class="material-icons text-sm mr-1">logout</span> Logout</a>
        </div>
    </div>
    
    <div class="p-4 md:px-8">
        <h2 class="text-2xl font-bold text-purple-900 mb-6">Social Tasks</h2>
        <div class="space-y-4">
            {% if not tasks %} <p class="text-gray-500 text-center py-8">No tasks available.</p> {% endif %}
            {% for t in tasks %}
            <div class="md3-card p-5 flex flex-col md:flex-row md:items-center justify-between gap-4">
                <div>
                    <h3 class="text-lg font-bold text-gray-800">{{ t.title }}</h3>
                    <p class="text-sm text-gray-500 mb-2">{{ t.desc }}</p>
                    <span class="bg-green-100 text-green-800 text-xs font-bold px-3 py-1 rounded-full">Reward: ₦{{ t.pay }}</span>
                </div>
                <div>
                    {% if t.id in user_subs and user_subs[t.id] != 'rejected' %}
                        <div class="px-4 py-2 rounded-full text-sm font-bold text-center bg-gray-100 text-gray-600 uppercase">{{ user_subs[t.id] }}</div>
                    {% else %}
                        <button type="button" onclick="document.getElementById('modal-t{{ t.id }}').classList.remove('hidden')" class="w-full md:w-auto bg-purple-600 text-white font-bold px-6 py-3 rounded-full hover:bg-purple-700">Do Task</button>
                    {% endif %}
                </div>
            </div>

            <div id="modal-t{{ t.id }}" class="hidden fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4 backdrop-blur-sm">
                <div class="bg-white rounded-[32px] w-full max-w-md p-6">
                    <div class="flex justify-between items-center mb-4">
                        <h3 class="text-xl font-bold text-purple-900">Complete Task</h3>
                        <button type="button" onclick="document.getElementById('modal-t{{ t.id }}').classList.add('hidden')" class="bg-gray-100 rounded-full p-2"><span class="material-icons">close</span></button>
                    </div>
                    <div class="mb-6">
                        <a href="{{ t.link }}" target="_blank" data-no-spa class="bg-purple-100 text-purple-800 px-4 py-2 rounded-lg font-bold block text-center mb-4 hover:bg-purple-200">1. Open Task Link Here</a>
                        <p class="text-sm text-gray-600">2. Complete task and upload proof below.</p>
                    </div>
                    <form method="POST" enctype="multipart/form-data" class="space-y-4">
                        <input type="hidden" name="task_id" value="{{ t.id }}">
                        
                        <div>
                            <p class="text-sm font-bold text-gray-700 mb-2">Upload Task Proof</p>
                            <label class="flex flex-col items-center justify-center w-full h-32 border-2 border-dashed border-purple-300 rounded-2xl cursor-pointer bg-purple-50 hover:bg-purple-100 transition-colors text-center px-4">
                                <div class="file-display flex flex-col items-center justify-center pointer-events-none">
                                    <span class="material-icons text-purple-400 mb-2 text-3xl">cloud_upload</span>
                                    <p class="mb-1 text-sm text-gray-500"><span class="font-bold text-purple-600">Click to upload</span> screenshot</p>
                                    <p class="text-xs text-gray-400">PNG, JPG or JPEG</p>
                                </div>
                                <input type="file" name="proof" required accept="image/*" class="hidden" onchange="compressAndPreview(this)">
                            </label>
                        </div>
                        
                        <button type="submit" class="w-full bg-purple-600 text-white font-bold py-4 rounded-full mt-2">Submit Task</button>
                    </form>
                </div>
            </div>
            {% endfor %}
        </div>
    </div>
    {% endblock %}
    """
    return render_page(content, tasks=all_tasks, user_subs=user_subs)

@app.route('/withdrawals', methods=['GET', 'POST'])
def withdrawals():
    username, user = get_user()
    if not user: return redirect(url_for('landing'))

    settings = get_settings()

    if request.method == 'POST':
        with get_db() as conn:
            if 'save_bank' in request.form:
                conn.execute("UPDATE users SET bank_name=?, acc_no=?, acc_name=? WHERE username=?",
                             (request.form['bank_name'], request.form['acc_no'], request.form['acc_name'], username))
                conn.commit()
                flash("Bank details saved", "success")
                
            elif 'withdraw' in request.form:
                w_type = request.form['type']
                amount = int(request.form['amount'])
                
                fresh_user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
                min_withdrawals = get_min_withdrawals()
                min_amt = min_withdrawals.get(f'min_withdraw_{w_type}', 0)
                
                if not fresh_user['acc_no']:
                    flash("Please save bank details first.", "error")
                elif amount <= 0 or amount > fresh_user[f'bal_{w_type}']:
                    flash("Invalid amount or insufficient balance.", "error")
                elif amount < min_amt:
                    flash(f"Minimum withdrawal for {w_type} is ₦{min_amt}.", "error")
                elif not settings.get(f'withdraw_{w_type}', False):
                    flash(f"{w_type.title()} withdrawal is currently closed by Admin.", "error")
                elif w_type == 'affiliate' and fresh_user['ref_tasks_count'] < 7:
                    flash(f"Referrals must complete 7 tasks. Current: {fresh_user['ref_tasks_count']}/7", "error")
                else:
                    conn.execute(f"UPDATE users SET bal_{w_type} = bal_{w_type} - ? WHERE username=?", (amount, username))
                    conn.execute("""INSERT INTO withdrawals (username, type, amount, status, date, bank_name, acc_no, acc_name) 
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                 (username, w_type, amount, 'pending', datetime.now().strftime("%Y-%m-%d"), 
                                  fresh_user['bank_name'], fresh_user['acc_no'], fresh_user['acc_name']))
                    conn.commit()
                    flash("Withdrawal requested successfully!", "success")
                    
        return redirect(url_for('withdrawals'))

    _, user = get_user()
    with get_db() as conn:
        my_with = conn.execute("SELECT * FROM withdrawals WHERE username=?", (username,)).fetchall()

    content = """
    {% block content %}
    <div class="hidden md:flex bg-purple-700 text-white px-8 py-4 justify-between items-center shadow-md mb-6 rounded-b-[24px]">
        <h1 class="text-2xl font-bold">NexFund</h1>
        <div class="flex space-x-6 font-medium">
            <a href="/dashboard" class="text-purple-200 hover:text-white pb-1">Dashboard</a>
            <a href="/investments" class="text-purple-200 hover:text-white pb-1">Plans</a>
            <a href="/tasks" class="text-purple-200 hover:text-white pb-1">Tasks</a>
            <a href="/withdrawals" class="text-white border-b-2 border-white pb-1">Withdraw</a>
            <a href="/logout" data-no-spa class="text-purple-200 hover:text-white flex items-center"><span class="material-icons text-sm mr-1">logout</span> Logout</a>
        </div>
    </div>

    <div class="p-4 md:px-8">
        <h2 class="text-2xl font-bold text-purple-900 mb-6">Withdraw Funds</h2>
        
        <div class="md3-card p-6 mb-6">
            <h3 class="font-bold text-gray-800 mb-4 flex items-center gap-2"><span class="material-icons">account_balance</span> Bank Details</h3>
            <form method="POST" class="space-y-3">
                <input type="hidden" name="save_bank" value="1">
                <input type="text" name="bank_name" placeholder="Bank Name" value="{{ user.bank_name }}" required class="w-full bg-gray-50 border rounded-xl px-4 py-3 outline-none focus:ring-2 focus:ring-purple-200">
                <input type="text" name="acc_no" placeholder="Account Number" value="{{ user.acc_no }}" required class="w-full bg-gray-50 border rounded-xl px-4 py-3 outline-none focus:ring-2 focus:ring-purple-200">
                <input type="text" name="acc_name" placeholder="Account Name" value="{{ user.acc_name }}" required class="w-full bg-gray-50 border rounded-xl px-4 py-3 outline-none focus:ring-2 focus:ring-purple-200">
                <button type="submit" class="bg-purple-100 text-purple-700 font-bold px-6 py-2 rounded-full">Save Details</button>
            </form>
        </div>

        <div class="bg-purple-600 rounded-[24px] p-6 shadow-md mb-8 text-white">
            <h3 class="font-bold mb-4 text-lg">Place Withdrawal</h3>
            <form method="POST" class="space-y-4">
                <input type="hidden" name="withdraw" value="1">
                <div>
                    <label class="block text-sm text-purple-100 mb-1">Select Balance</label>
                    <select name="type" class="w-full bg-purple-700 border border-purple-500 rounded-xl px-4 py-3 outline-none text-white">
                        <option value="affiliate">Affiliate (₦{{ user.bal_affiliate }})</option>
                        <option value="invest">Investment (₦{{ user.bal_invest }})</option>
                        <option value="task">Task (₦{{ user.bal_task }})</option>
                    </select>
                </div>
                <div>
                    <label class="block text-sm text-purple-100 mb-1">Amount (₦)</label>
                    <input type="number" name="amount" required class="w-full bg-purple-700 border border-purple-500 rounded-xl px-4 py-3 outline-none text-white placeholder-purple-300" placeholder="Enter amount">
                </div>
                <button type="submit" class="w-full bg-white text-purple-700 font-bold py-3 rounded-full hover:bg-purple-50 transition-colors">Withdraw</button>
            </form>
            {% if user.ref_tasks_count < 7 %}
            <p class="text-xs text-yellow-300 mt-3"><span class="material-icons text-[14px] align-middle">warning</span> Affiliate lock: Referrals completed {{ user.ref_tasks_count }}/7 tasks.</p>
            {% endif %}
        </div>

        {% if my_with %}
        <h3 class="text-lg font-bold text-gray-800 mb-4">History</h3>
        <div class="space-y-3">
            {% for w in my_with|reverse %}
            <div class="md3-card p-4 flex justify-between items-center">
                <div>
                    <p class="font-bold text-gray-800">₦{{ w.amount }} <span class="text-xs text-gray-500">({{ w.type }})</span></p>
                    <p class="text-xs text-gray-400">{{ w.date }}</p>
                </div>
                <span class="px-3 py-1 rounded-full text-xs font-bold {% if w.status=='approved' %}bg-green-100 text-green-700{% elif w.status=='rejected' %}bg-red-100 text-red-700{% else %}bg-orange-100 text-orange-700{% endif %}">
                    {{ w.status | upper }}
                </span>
            </div>
            {% endfor %}
        </div>
        {% endif %}
    </div>
    {% endblock %}
    """
    return render_page(content, user=user, my_with=my_with)

@app.route('/notifications')
def notifications():
    username, user = get_user()
    if not user: return redirect(url_for('landing'))

    with get_db() as conn:
        notes = conn.execute("SELECT * FROM notifications WHERE username=? ORDER BY id DESC", (username,)).fetchall()
        conn.execute("UPDATE notifications SET read=1 WHERE username=? AND read=0", (username,))
        conn.commit()

    content = """
    {% block content %}
    <div class="hidden md:flex bg-purple-700 text-white px-8 py-4 justify-between items-center shadow-md mb-6 rounded-b-[24px]">
        <h1 class="text-2xl font-bold">NexFund</h1>
        <div class="flex space-x-6 font-medium">
            <a href="/dashboard" class="text-purple-200 hover:text-white pb-1">Dashboard</a>
            <a href="/investments" class="text-purple-200 hover:text-white pb-1">Plans</a>
            <a href="/tasks" class="text-purple-200 hover:text-white pb-1">Tasks</a>
            <a href="/withdrawals" class="text-purple-200 hover:text-white pb-1">Withdraw</a>
            <a href="/logout" data-no-spa class="text-purple-200 hover:text-white flex items-center"><span class="material-icons text-sm mr-1">logout</span> Logout</a>
        </div>
    </div>

    <div class="p-4 md:px-8">
        <h2 class="text-2xl font-bold text-purple-900 mb-6">Notifications</h2>
        <div class="space-y-3">
            {% for n in notes %}
            <div class="md3-card p-4">
                <p class="text-gray-800">{{ n.message }}</p>
                <p class="text-xs text-gray-400 mt-2">{{ n.date }}</p>
            </div>
            {% else %}
            <div class="md3-card p-6 text-center text-gray-400">No notifications yet.</div>
            {% endfor %}
        </div>
    </div>
    {% endblock %}
    """
    return render_page(content, notes=notes)

@app.route('/admin', methods=['GET', 'POST'])
def admin_dashboard():
    if session.get('user') != 'admin': return redirect(url_for('landing'))
    
    if request.method == 'POST':
        req_action = request.form.get('req_action')
        
        with get_db() as conn:
            if req_action == 'add_task':
                conn.execute("INSERT INTO tasks (title, desc, link, pay) VALUES (?, ?, ?, ?)",
                             (request.form['title'], request.form['desc'], request.form['link'], int(request.form['pay'])))
                flash("Task Created", "success")
                
            elif req_action == 'edit_task':
                tid = int(request.form['id'])
                conn.execute("UPDATE tasks SET title=?, desc=?, link=?, pay=? WHERE id=?",
                             (request.form['title'], request.form['desc'], request.form['link'], int(request.form['pay']), tid))
                flash("Task Updated", "success")

            elif req_action == 'delete_task':
                tid = int(request.form['id'])
                conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
                flash("Task Deleted", "success")

            elif req_action == 'approve_task':
                tid, status = int(request.form['id']), request.form['status']
                ut = conn.execute("SELECT ut.username, ut.proof_b64, t.pay FROM user_tasks ut LEFT JOIN tasks t ON ut.task_id = t.id WHERE ut.id=?", (tid,)).fetchone()
                
                if ut:
                    conn.execute("UPDATE user_tasks SET status=? WHERE id=?", (status, tid))
                    if status == 'approved':
                        pay = ut['pay'] or 0
                        conn.execute("UPDATE users SET bal_task = bal_task + ? WHERE username=?", (pay, ut['username']))
                        user = conn.execute("SELECT referred_by FROM users WHERE username=?", (ut['username'],)).fetchone()
                        if user and user['referred_by']:
                            conn.execute("UPDATE users SET ref_tasks_count = ref_tasks_count + 1 WHERE username=?", (user['referred_by'],))
                    delete_github_file(ut['proof_b64'])
                    send_notification(ut['username'], f"Your task submission was {status}.", conn=conn)
                    flash(f"Task {status}", "success")
                else:
                    flash("This submission's task no longer exists.", "error")
            
            elif req_action == 'approve_invest':
                iid, status = int(request.form['id']), request.form['status']
                inv = conn.execute("SELECT username, proof_b64, plan_id FROM investments WHERE id=?", (iid,)).fetchone()
                # Reset date to now so the 24-hour earning clock starts fresh at approval time
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                conn.execute("UPDATE investments SET status=?, date=? WHERE id=?", (status, now_str, iid))
                if inv:
                    delete_github_file(inv['proof_b64'])
                    send_notification(inv['username'], f"Your investment plan payment was {status}.", conn=conn)
                    if status == 'approved':
                        buyer = conn.execute("SELECT referred_by FROM users WHERE username=?", (inv['username'],)).fetchone()
                        if buyer and buyer['referred_by']:
                            plan = get_plan_by_id(inv['plan_id'])
                            if plan:
                                bonus = round(plan['price'] * 0.05)
                                conn.execute("UPDATE users SET bal_affiliate = bal_affiliate + ? WHERE username=?", (bonus, buyer['referred_by']))
                                send_notification(buyer['referred_by'], f"You earned ₦{bonus} (5%) referral bonus from {inv['username']}'s plan purchase.", conn=conn)
                flash(f"Investment {status}", "success")
                
            elif req_action == 'approve_withdraw':
                wid, status = int(request.form['id']), request.form['status']
                w = conn.execute("SELECT username, amount, type FROM withdrawals WHERE id=?", (wid,)).fetchone()
                
                if w:
                    conn.execute("UPDATE withdrawals SET status=? WHERE id=?", (status, wid))
                    if status == 'rejected':
                        conn.execute(f"UPDATE users SET bal_{w['type']} = bal_{w['type']} + ? WHERE username=?", (w['amount'], w['username']))
                    send_notification(w['username'], f"Your withdrawal of ₦{w['amount']} was {status}.", conn=conn)
                flash(f"Withdrawal {status}", "success")
                
            elif req_action == 'toggle_settings':
                stype = request.form['type']
                conn.execute("UPDATE settings SET value = 1 - value WHERE key=?", (stype,))
                flash("Setting toggled", "success")

            elif req_action == 'set_min_withdraw':
                for btype in ('affiliate', 'invest', 'task'):
                    amt = int(request.form.get(f'min_withdraw_{btype}', 0) or 0)
                    conn.execute("UPDATE settings SET value=? WHERE key=?", (amt, f'min_withdraw_{btype}'))
                flash("Minimum withdrawal amounts updated", "success")

            elif req_action == 'edit_balance':
                uname = request.form['username']
                conn.execute("UPDATE users SET bal_affiliate=?, bal_invest=?, bal_task=? WHERE username=?",
                             (int(request.form['bal_affiliate']), int(request.form['bal_invest']), int(request.form['bal_task']), uname))
                flash(f"Balance updated for {uname}", "success")

            elif req_action == 'toggle_ban':
                uname = request.form['username']
                conn.execute("UPDATE users SET banned = 1 - banned WHERE username=?", (uname,))
                flash(f"Ban status toggled for {uname}", "success")

            elif req_action == 'activate_plan':
                uname = request.form['username']
                pid = int(request.form['plan_id'])
                conn.execute("INSERT INTO investments (username, plan_id, proof_b64, status, date, days_elapsed, sender_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (uname, pid, '', 'approved', datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 0, 'Admin Activated'))
                send_notification(uname, "An investment plan was activated for you by admin.", conn=conn)
                flash(f"Plan activated for {uname}", "success")

            elif req_action == 'send_notification':
                msg = request.form['message'].strip()
                if msg:
                    broadcast_notification(msg, conn=conn)
                    flash("Notification broadcasted", "success")
                
            conn.commit()

    with get_db() as conn:
        tot_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        tot_bal = conn.execute("SELECT SUM(bal_affiliate + bal_invest + bal_task) FROM users").fetchone()[0] or 0
        settings = get_settings()
        
        all_tasks = conn.execute("SELECT * FROM tasks").fetchall()
        task_counts = {}
        for row in conn.execute("SELECT task_id, status, COUNT(*) as cnt FROM user_tasks GROUP BY task_id, status").fetchall():
            entry = task_counts.setdefault(row['task_id'], {'completed': 0, 'approved': 0})
            entry['completed'] += row['cnt']
            if row['status'] == 'approved':
                entry['approved'] = row['cnt']
        
        pending_tasks = conn.execute("""
            SELECT ut.*, t.title as task_title 
            FROM user_tasks ut 
            LEFT JOIN tasks t ON ut.task_id = t.id 
            WHERE ut.status='pending'
        """).fetchall()
        
        pending_invests_raw = conn.execute("SELECT * FROM investments WHERE status='pending'").fetchall()
        pending_invests = []
        for i in pending_invests_raw:
            row = dict(i)
            plan = get_plan_by_id(row['plan_id'])
            row['plan_name'] = plan['name'] if plan else f"Unknown Plan ({row['plan_id']})"
            row['plan_price'] = plan['price'] if plan else None
            pending_invests.append(row)
        pending_withdraws = conn.execute("SELECT * FROM withdrawals WHERE status='pending'").fetchall()
        min_withdrawals = get_min_withdrawals()

        user_search = request.args.get('user_search', '').strip()
        if user_search:
            searched_users = conn.execute("SELECT * FROM users WHERE username LIKE ?", (f"%{user_search}%",)).fetchall()
        else:
            searched_users = []

    content = """
    {% block content %}
    <div id="admin-wrapper" class="p-4 pt-6 max-w-5xl mx-auto">
        <div class="flex justify-between items-center bg-gray-900 text-white p-6 rounded-[24px] mb-6">
            <div><h2 class="text-2xl font-bold">Admin</h2><p class="text-purple-300 text-sm">Manager Portal</p></div>
            <a href="/logout" data-no-spa class="bg-red-500/20 text-red-400 px-4 py-2 rounded-full font-bold text-sm">Logout</a>
        </div>

        <div id="tab-dash" class="admin-tab block space-y-6">
            <div class="grid grid-cols-2 gap-4">
                <div class="md3-card p-5"><p class="text-xs text-gray-500">Users</p><p class="text-2xl font-bold">{{ tot_users }}</p></div>
                <div class="md3-card p-5"><p class="text-xs text-gray-500">Balances</p><p class="text-2xl font-bold text-purple-600">₦{{ tot_bal }}</p></div>
            </div>
            
            <div class="md3-card p-6">
                <h3 class="font-bold text-gray-800 mb-4">Withdrawal Gates</h3>
                <div class="flex flex-col space-y-3">
                    {% for k, v in settings.items() if k.startswith('withdraw_') %}
                    <form method="POST" class="flex justify-between items-center bg-gray-50 p-3 rounded-xl border">
                        <input type="hidden" name="req_action" value="toggle_settings"><input type="hidden" name="type" value="{{ k }}">
                        <span class="text-sm font-medium text-gray-700 capitalize">{{ k | replace('withdraw_', '') }} Gate</span>
                        <button type="submit" class="w-14 h-8 rounded-full relative transition-colors duration-300 {% if v %}bg-green-500{% else %}bg-gray-300{% endif %}">
                            <div class="absolute top-1 w-6 h-6 rounded-full bg-white transition-transform duration-300 shadow-sm {% if v %}left-7{% else %}left-1{% endif %}"></div>
                        </button>
                    </form>
                    {% endfor %}
                </div>
            </div>

            <div class="md3-card p-6">
                <h3 class="font-bold text-gray-800 mb-4">Minimum Withdrawal Amounts</h3>
                <form method="POST" class="flex flex-col gap-3">
                    <input type="hidden" name="req_action" value="set_min_withdraw">
                    {% for btype in ['affiliate', 'invest', 'task'] %}
                    <label class="text-sm font-medium text-gray-700 capitalize">{{ btype }} Balance Minimum (₦)</label>
                    <input type="number" name="min_withdraw_{{ btype }}" value="{{ min_withdrawals.get('min_withdraw_' + btype, 0) }}" min="0" class="bg-gray-50 border p-3 rounded-xl outline-none focus:ring-2 focus:ring-purple-200">
                    {% endfor %}
                    <button type="submit" class="bg-purple-600 text-white font-bold py-3 rounded-xl">Save Minimums</button>
                </form>
            </div>

            <div class="md3-card p-6">
                <h3 class="font-bold text-gray-800 mb-4">Broadcast Notification</h3>
                <form method="POST" class="flex flex-col gap-3">
                    <input type="hidden" name="req_action" value="send_notification">
                    <textarea name="message" required placeholder="Type a message to send to all users..." class="bg-gray-50 border p-3 rounded-xl outline-none focus:ring-2 focus:ring-purple-200 h-24"></textarea>
                    <button type="submit" class="bg-purple-600 text-white font-bold py-3 rounded-xl">Send to All Users</button>
                </form>
            </div>
        </div>

        <div id="tab-tasks" class="admin-tab hidden space-y-6">
            <div class="md3-card p-6">
                <h3 class="font-bold text-gray-800 mb-4">Create New Task</h3>
                <form method="POST" class="flex flex-col gap-3">
                    <input type="hidden" name="req_action" value="add_task">
                    <input type="text" name="title" placeholder="Task Title" required class="bg-gray-50 border p-3 rounded-xl outline-none focus:ring-2 focus:ring-purple-200">
                    <input type="number" name="pay" placeholder="Pay Amount (₦)" required class="bg-gray-50 border p-3 rounded-xl outline-none focus:ring-2 focus:ring-purple-200">
                    <input type="text" name="link" placeholder="Task Link (URL)" required class="bg-gray-50 border p-3 rounded-xl outline-none focus:ring-2 focus:ring-purple-200">
                    <input type="text" name="desc" placeholder="Short Description" class="bg-gray-50 border p-3 rounded-xl outline-none focus:ring-2 focus:ring-purple-200">
                    <button type="submit" class="bg-purple-600 text-white font-bold py-3 rounded-xl mt-2">Publish Task</button>
                </form>
            </div>
            
            <div class="md3-card p-6">
                <h3 class="font-bold text-gray-800 mb-4">Active Tasks</h3>
                <div class="space-y-4">
                    {% for t in all_tasks %}
                    <div class="border p-4 rounded-xl flex flex-col md:flex-row justify-between md:items-center bg-gray-50 gap-4">
                        <div>
                            <h4 class="font-bold text-gray-800">{{ t.title }}</h4>
                            <p class="text-sm text-gray-600 font-medium">Pay: <span class="text-green-600">₦{{ t.pay }}</span></p>
                            <p class="text-xs text-gray-500 mt-1">Completed: <span class="font-bold">{{ task_counts.get(t.id, {}).get('completed', 0) }}</span> &middot; Approved: <span class="font-bold text-green-600">{{ task_counts.get(t.id, {}).get('approved', 0) }}</span></p>
                        </div>
                        <div class="flex gap-2">
                            <button type="button" onclick="document.getElementById('edit-task-{{ t.id }}').classList.remove('hidden')" class="flex-1 md:flex-none bg-blue-100 text-blue-700 px-4 py-2 rounded-lg font-bold text-sm">Edit</button>
                            <form method="POST" class="flex-1 md:flex-none inline m-0">
                                <input type="hidden" name="req_action" value="delete_task">
                                <input type="hidden" name="id" value="{{ t.id }}">
                                <button type="submit" class="w-full bg-red-100 text-red-700 px-4 py-2 rounded-lg font-bold text-sm">Delete</button>
                            </form>
                        </div>
                    </div>
                    
                    <div id="edit-task-{{ t.id }}" class="hidden fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4 backdrop-blur-sm">
                        <div class="bg-white rounded-[32px] w-full max-w-md p-6">
                            <div class="flex justify-between items-center mb-4">
                                <h3 class="text-xl font-bold text-purple-900">Edit Task</h3>
                                <button type="button" onclick="document.getElementById('edit-task-{{ t.id }}').classList.add('hidden')" class="bg-gray-100 rounded-full p-2"><span class="material-icons">close</span></button>
                            </div>
                            <form method="POST" class="flex flex-col gap-3">
                                <input type="hidden" name="req_action" value="edit_task">
                                <input type="hidden" name="id" value="{{ t.id }}">
                                <label class="text-xs font-bold text-gray-500 ml-1">Title</label>
                                <input type="text" name="title" value="{{ t.title }}" required class="bg-gray-50 border p-3 rounded-xl outline-none focus:ring-2 focus:ring-purple-200">
                                <label class="text-xs font-bold text-gray-500 ml-1">Pay (₦)</label>
                                <input type="number" name="pay" value="{{ t.pay }}" required class="bg-gray-50 border p-3 rounded-xl outline-none focus:ring-2 focus:ring-purple-200">
                                <label class="text-xs font-bold text-gray-500 ml-1">Link</label>
                                <input type="text" name="link" value="{{ t.link }}" required class="bg-gray-50 border p-3 rounded-xl outline-none focus:ring-2 focus:ring-purple-200">
                                <label class="text-xs font-bold text-gray-500 ml-1">Description</label>
                                <input type="text" name="desc" value="{{ t.desc }}" class="bg-gray-50 border p-3 rounded-xl outline-none focus:ring-2 focus:ring-purple-200">
                                <button type="submit" class="bg-purple-600 text-white font-bold py-3 rounded-xl mt-4 shadow-md">Save Changes</button>
                            </form>
                        </div>
                    </div>
                    {% else %}
                    <p class="text-gray-500 text-sm">No active tasks.</p>
                    {% endfor %}
                </div>
            </div>
            
            <h3 class="font-bold text-gray-800 mt-8 mb-2">Pending Task Submissions</h3>
            {% for t in pending_tasks %}
            <div class="md3-card p-4">
                <p class="font-bold text-purple-900 mb-1">{{ t.task_title or 'Unknown Task' }} <span class="text-gray-500 font-normal text-sm block">User: {{ t.username }}</span></p>
                <img src="{{ t.proof_b64 }}" onclick="openFullscreen(this.src)" class="w-full h-40 object-cover rounded-xl mt-2 border bg-gray-50 cursor-zoom-in hover:opacity-90 transition-opacity">
                <div class="flex gap-2 mt-4">
                    <form method="POST" class="flex-1">
                        <input type="hidden" name="req_action" value="approve_task"><input type="hidden" name="id" value="{{ t.id }}"><input type="hidden" name="status" value="approved">
                        <button type="submit" class="w-full bg-green-100 text-green-700 font-bold py-2 rounded-lg">Approve</button>
                    </form>
                    <form method="POST" class="flex-1">
                        <input type="hidden" name="req_action" value="approve_task"><input type="hidden" name="id" value="{{ t.id }}"><input type="hidden" name="status" value="rejected">
                        <button type="submit" class="w-full bg-red-100 text-red-700 font-bold py-2 rounded-lg">Reject</button>
                    </form>
                </div>
            </div>
            {% else %}
            <div class="md3-card p-6 text-center text-gray-400">No pending tasks.</div>
            {% endfor %}
        </div>

        <div id="tab-invests" class="admin-tab hidden space-y-4">
            <h3 class="font-bold text-gray-800 mb-2">Pending Investments</h3>
            {% for i in pending_invests %}
            <div class="md3-card p-4">
                <div class="flex justify-between items-center mb-2">
                    <p class="font-bold text-purple-900">{{ i.username }}</p>
                    <span class="bg-purple-100 text-purple-800 text-xs font-bold px-2 py-1 rounded">{{ i.plan_name }}{% if i.plan_price %} (₦{{ i.plan_price }}){% endif %}</span>
                </div>
                <p class="text-xs text-gray-500 mb-2">Sender Name: <span class="font-bold text-gray-700">{{ i.sender_name or 'N/A' }}</span></p>
                <img src="{{ i.proof_b64 }}" onclick="openFullscreen(this.src)" class="w-full h-40 object-cover rounded-xl mt-2 border bg-gray-50 cursor-zoom-in hover:opacity-90 transition-opacity">
                <div class="flex gap-2 mt-4">
                    <form method="POST" class="flex-1">
                        <input type="hidden" name="req_action" value="approve_invest"><input type="hidden" name="id" value="{{ i.id }}"><input type="hidden" name="status" value="approved">
                        <button type="submit" class="w-full bg-green-100 text-green-700 font-bold py-2 rounded-lg">Approve</button>
                    </form>
                    <form method="POST" class="flex-1">
                        <input type="hidden" name="req_action" value="approve_invest"><input type="hidden" name="id" value="{{ i.id }}"><input type="hidden" name="status" value="rejected">
                        <button type="submit" class="w-full bg-red-100 text-red-700 font-bold py-2 rounded-lg">Reject</button>
                    </form>
                </div>
            </div>
            {% else %}
            <div class="md3-card p-6 text-center text-gray-400">No pending investments.</div>
            {% endfor %}
        </div>

        <div id="tab-withdraws" class="admin-tab hidden space-y-4">
            <h3 class="font-bold text-gray-800 mb-2">Pending Withdrawals</h3>
            {% for w in pending_withdraws %}
            <div class="md3-card p-5 border-l-4 border-l-purple-500">
                <div class="flex justify-between items-start mb-2">
                    <div>
                        <p class="font-extrabold text-2xl text-gray-800">₦{{ w.amount }}</p>
                        <p class="text-sm font-bold text-purple-600">{{ w.username }}</p>
                    </div>
                    <span class="bg-gray-100 text-gray-600 text-[10px] uppercase font-bold px-2 py-1 rounded">{{ w.type }}</span>
                </div>
                
                <div class="bg-purple-50 p-3 mt-3 rounded-xl border border-purple-100 text-sm">
                    <p class="text-purple-800 font-medium">{{ w.bank_name }}</p>
                    <div class="flex items-center gap-2">
                        <p class="font-bold text-lg text-purple-900 tracking-wider">{{ w.acc_no }}</p>
                        <button type="button" onclick="navigator.clipboard.writeText('{{ w.acc_no }}'); showJSToast('Account number copied!');" class="text-purple-600 bg-purple-100 rounded-full p-1 hover:bg-purple-200"><span class="material-icons text-base">content_copy</span></button>
                    </div>
                    <p class="text-purple-800 uppercase text-xs font-bold">{{ w.acc_name }}</p>
                </div>

                <div class="flex gap-2 mt-4">
                    <form method="POST" class="flex-1">
                        <input type="hidden" name="req_action" value="approve_withdraw"><input type="hidden" name="id" value="{{ w.id }}"><input type="hidden" name="status" value="approved">
                        <button type="submit" class="w-full bg-green-100 text-green-700 font-bold py-2 rounded-lg">Approve</button>
                    </form>
                    <form method="POST" class="flex-1">
                        <input type="hidden" name="req_action" value="approve_withdraw"><input type="hidden" name="id" value="{{ w.id }}"><input type="hidden" name="status" value="rejected">
                        <button type="submit" class="w-full bg-red-100 text-red-700 font-bold py-2 rounded-lg">Reject</button>
                    </form>
                </div>
            </div>
            {% else %}
            <div class="md3-card p-6 text-center text-gray-400">No pending withdrawals.</div>
            {% endfor %}
        </div>

        <div id="tab-users" class="admin-tab hidden space-y-4">
            <div class="md3-card p-4">
                <form method="GET" class="flex gap-2 w-full">
                    <input type="text" name="user_search" value="{{ request.args.get('user_search','') }}" placeholder="Search username..." class="flex-1 min-w-0 bg-gray-50 border p-3 rounded-xl outline-none focus:ring-2 focus:ring-purple-200">
                    <button type="submit" class="shrink-0 bg-purple-600 text-white font-bold px-5 rounded-xl">Search</button>
                </form>
            </div>

            {% for u in searched_users %}
            <div class="md3-card p-5">
                <div class="flex justify-between items-center mb-3">
                    <p class="font-bold text-purple-900">{{ u.username }}{% if u.banned %} <span class="bg-red-100 text-red-700 text-[10px] font-bold px-2 py-1 rounded-full uppercase align-middle">Banned</span>{% endif %}</p>
                    <form method="POST" class="m-0">
                        <input type="hidden" name="req_action" value="toggle_ban"><input type="hidden" name="username" value="{{ u.username }}">
                        <button type="submit" class="text-xs font-bold px-3 py-2 rounded-full {% if u.banned %}bg-green-100 text-green-700{% else %}bg-red-100 text-red-700{% endif %}">{% if u.banned %}Unban{% else %}Ban{% endif %}</button>
                    </form>
                </div>

                <form method="POST" class="grid grid-cols-3 gap-2 mb-3">
                    <input type="hidden" name="req_action" value="edit_balance"><input type="hidden" name="username" value="{{ u.username }}">
                    <div>
                        <label class="text-[10px] text-gray-500 font-bold uppercase">Affiliate</label>
                        <input type="number" name="bal_affiliate" value="{{ u.bal_affiliate }}" class="w-full bg-gray-50 border p-2 rounded-lg text-sm">
                    </div>
                    <div>
                        <label class="text-[10px] text-gray-500 font-bold uppercase">Invest</label>
                        <input type="number" name="bal_invest" value="{{ u.bal_invest }}" class="w-full bg-gray-50 border p-2 rounded-lg text-sm">
                    </div>
                    <div>
                        <label class="text-[10px] text-gray-500 font-bold uppercase">Task</label>
                        <input type="number" name="bal_task" value="{{ u.bal_task }}" class="w-full bg-gray-50 border p-2 rounded-lg text-sm">
                    </div>
                    <button type="submit" class="col-span-3 bg-purple-100 text-purple-700 font-bold py-2 rounded-lg text-sm">Save Balances</button>
                </form>

                <form method="POST" class="flex gap-2">
                    <input type="hidden" name="req_action" value="activate_plan"><input type="hidden" name="username" value="{{ u.username }}">
                    <select name="plan_id" class="flex-1 bg-gray-50 border p-2 rounded-lg text-sm">
                        <optgroup label="10 Day Plans">
                        {% for p in plans if p.tab == '10day' %}
                        <option value="{{ p.id }}">{{ p.name }} (₦{{ p.price }})</option>
                        {% endfor %}
                        </optgroup>
                        <optgroup label="Monthly Plans">
                        {% for p in plans if p.tab == 'monthly' %}
                        <option value="{{ p.id }}">{{ p.name }} (₦{{ p.price }})</option>
                        {% endfor %}
                        </optgroup>
                    </select>
                    <button type="submit" class="bg-green-100 text-green-700 font-bold px-4 rounded-lg text-sm">Activate Plan</button>
                </form>
            </div>
            {% else %}
            <div class="md3-card p-6 text-center text-gray-400">{% if request.args.get('user_search') %}No users found.{% else %}Search for a user above.{% endif %}</div>
            {% endfor %}
        </div>
    </div>
    {% endblock %}
    """
    return render_page(content, db=settings, settings=settings, all_tasks=all_tasks, tot_users=tot_users, tot_bal=tot_bal, pending_tasks=pending_tasks, pending_invests=pending_invests, pending_withdraws=pending_withdraws, searched_users=searched_users, plans=PLANS, task_counts=task_counts, min_withdrawals=min_withdrawals)

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('landing'))

if __name__ == '__main__':
    import os
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5022)), debug=False, threaded=False, use_reloader=False)
