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
  document.getElementById('loginButton').addEventListener('click', () => dialog.showModal());
  document.getElementById('closeDialog').addEventListener('click', () => dialog.close());
  document.getElementById('gotIt').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', event => {
    if (event.target === dialog) dialog.close();
  });
})();