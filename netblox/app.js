(() => {
  const search = document.getElementById('searchInput');
  const cards = [...document.querySelectorAll('.game-card')];
  const filters = [...document.querySelectorAll('.filter')];
  const empty = document.getElementById('emptyState');
  let category = 'All';
  function applyFilters() {
    const term = (search.value || '').trim().toLowerCase();
    let visible = 0;
    cards.forEach(card => {
      const matchesText = card.dataset.title.toLowerCase().includes(term);
      const matchesCategory = category === 'All' || card.dataset.category === category;
      card.hidden = !(matchesText && matchesCategory);
      if (!card.hidden) visible++;
    });
    empty.hidden = visible !== 0;
  }
  search.addEventListener('input', applyFilters);
  filters.forEach(button => button.addEventListener('click', () => {
    category = button.dataset.filter;
    filters.forEach(item => item.classList.toggle('active', item === button));
    applyFilters();
  }));
  const dialog = document.getElementById('loginDialog');
  const form = document.getElementById('authForm');
  const title = document.getElementById('authTitle');
  const intro = document.getElementById('authIntro');
  const submit = document.getElementById('authSubmit');
  const error = document.getElementById('authError');
  const account = document.getElementById('accountState');
  const tabWrap = document.querySelector('.auth-tabs');
  const tabs = [document.getElementById('showLogin'), document.getElementById('showRegister')];
  let mode = 'login', busy = false;
  function setMode(next) {
    mode = next;
    tabs.forEach((tab, i) => tab.classList.toggle('active', i === (next === 'login' ? 0 : 1)));
    title.textContent = next === 'login' ? 'Welcome back' : 'Create your account';
    intro.textContent = next === 'login' ? 'Sign in to continue to Netblox.' : 'Choose a username and a strong password. You will stay signed in on this device.';
    submit.textContent = next === 'login' ? 'Sign in' : 'Create account';
    document.getElementById('authPassword').autocomplete = next === 'login' ? 'current-password' : 'new-password';
    document.getElementById('authPassword').value = '';
    error.hidden = true;
  }
  function showSignedIn(user) {
    form.hidden = true; tabWrap.hidden = true; account.hidden = false;
    document.getElementById('accountGreeting').textContent = 'Signed in as ' + user.username;
    const button = document.getElementById('loginButton');
    button.textContent = user.username;
    button.setAttribute('aria-label', 'Account: ' + user.username);
  }
  function showSignedOut() {
    form.hidden = false; tabWrap.hidden = false; account.hidden = true;
    const button = document.getElementById('loginButton');
    button.textContent = '↗  Sign in'; button.setAttribute('aria-label','Sign in');
  }
  async function api(path, body) {
    const response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: body === undefined ? {'Accept':'application/json'} : {'Content-Type':'application/json','Accept':'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Something went wrong.');
    return data;
  }
  async function refreshAccount() {
    try { const data = await api('/api/auth/me'); if (data.authenticated) showSignedIn(data.user); else showSignedOut(); }
    catch (_) { showSignedOut(); }
  }
  document.getElementById('loginButton').addEventListener('click', () => {
    setMode('login'); refreshAccount().finally(() => dialog.showModal());
  });
  document.getElementById('closeDialog').addEventListener('click', () => dialog.close());
  tabs[0].addEventListener('click', () => setMode('login'));
  tabs[1].addEventListener('click', () => setMode('register'));
  document.getElementById('logoutButton').addEventListener('click', async () => {
    try { await api('/api/auth/logout', {}); showSignedOut(); setMode('login'); }
    catch (e) { error.textContent = e.message; error.hidden = false; }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (busy) return;
    busy = true; submit.disabled = true; submit.textContent = mode === 'login' ? 'Signing in…' : 'Creating…'; error.hidden = true;
    try {
      const data = await api('/api/auth/' + mode, {
        username: document.getElementById('authUsername').value,
        password: document.getElementById('authPassword').value
      });
      showSignedIn(data.user); form.reset();
    } catch (e) { error.textContent = e.message; error.hidden = false; }
    finally { busy = false; submit.disabled = false; submit.textContent = mode === 'login' ? 'Sign in' : 'Create account'; }
  });
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  refreshAccount();
})();