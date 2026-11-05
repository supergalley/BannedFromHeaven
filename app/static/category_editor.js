(() => {
  const form = document.getElementById('category-editor');
  if (!form) return;
  const fields = document.getElementById('category-editor-fields');
  const list = document.getElementById('category-list');
  const detail = document.getElementById('category-details');
  const search = document.getElementById('category-search');
  const save = document.getElementById('save-categories');
  const status = document.getElementById('category-editor-status');
  const selectCategories = document.getElementById('select-visible-categories');
  const removeCategories = document.getElementById('remove-categories');
  const selectedCategories = new Set();
  const selectedSubs = new Set();
  const subSearches = new Map();
  const clone = value => JSON.parse(JSON.stringify(value));
  let base;
  let categories = [];
  let baseCategories = new Map();
  let baseSubs = new Map();
  let activeKey;
  let busy = false;
  let counter = 0;

  function element(tag, className = '', text) {
    const node = document.createElement(tag);
    node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function button(text, className, action) {
    const node = element('button', className, text);
    node.type = 'button';
    node.addEventListener('click', action);
    return node;
  }
  function matches(item, term) {
    return (item.name + ' ' + item.slug).toLowerCase().includes(term);
  }
  function activeCategory() { return categories.find(c => c.key === activeKey); }
  function orderedCategories() {
    return [...categories].sort((a, b) => a.name.localeCompare(b.name) || a.key.localeCompare(b.key));
  }
  function visibleCategories() {
    const term = search.value.trim().toLowerCase();
    return orderedCategories().filter(c => matches(c, term) || c.subcategories.some(s => matches(s, term)));
  }
  function changed(item, original) {
    return !original || item.name !== original.name || item.slug !== original.slug;
  }
  function categoryChanged(category) {
    const original = baseCategories.get(category.id);
    return category.removed || changed(category, original) || category.subcategories.some(
      sub => sub.removed || changed(sub, baseSubs.get(sub.id)));
  }
  function changes() {
    const counts = {added: 0, edited: 0, categories: 0, subcategories: 0};
    categories.forEach(c => {
      if (c.removed) {
        if (c.id != null) {
          counts.categories++;
          counts.subcategories += baseCategories.get(c.id).subcategories.length;
        }
        return;
      }
      if (c.id == null) counts.added++;
      else if (changed(c, baseCategories.get(c.id))) counts.edited++;
      c.subcategories.forEach(s => {
        if (s.removed) { if (s.id != null) counts.subcategories++; }
        else if (s.id == null) counts.added++;
        else if (changed(s, baseSubs.get(s.id))) counts.edited++;
      });
    });
    counts.total = counts.added + counts.edited + counts.categories + counts.subcategories;
    return counts;
  }
  function message(text = '', error = false) {
    status.textContent = text;
    status.classList.toggle('text-warning', error);
  }
  function updateChangeBar() {
    const count = changes();
    const labels = [];
    if (count.added) labels.push(count.added + ' new');
    if (count.edited) labels.push(count.edited + ' edited');
    if (count.categories) labels.push(count.categories + ' categories to remove');
    if (count.subcategories) labels.push(count.subcategories + ' subcategories to remove');
    document.getElementById('category-change-count').textContent = labels.join(' · ') || 'No unsaved changes';
    save.disabled = busy || !count.total;
    save.textContent = busy ? 'Saving…' : 'Save all changes';
  }
  function rememberSelection(category) {
    if (!category || category.id == null) return;
    history.replaceState(null, '', '#cat-' + category.id);
    try { sessionStorage.setItem('admin-category-active', String(category.id)); } catch (_) { /* Optional preference. */ }
  }
  function chooseCategory(key, focusName = false) {
    activeKey = key;
    const category = activeCategory();
    rememberSelection(category);
    const term = search.value.trim().toLowerCase();
    if (category && term && !matches(category, term)) subSearches.set(key, term);
    renderList();
    renderDetail(false);
    if (focusName) detail.querySelector('[data-category-name]')?.focus();
  }
  function renderList() {
    const scroll = list.scrollTop;
    list.replaceChildren();
    const visible = visibleCategories();
    document.getElementById('category-total').textContent = '(' + categories.filter(c => !c.removed).length + ')';
    visible.forEach(category => {
      const row = element('div', 'category-list-row' + (category.key === activeKey ? ' is-active' : '') + (category.removed ? ' is-removed' : ''));
      const checkbox = element('input', 'form-check-input m-0');
      checkbox.type = 'checkbox';
      checkbox.disabled = Boolean(category.removed);
      checkbox.checked = selectedCategories.has(category.key);
      checkbox.setAttribute('aria-label', 'Select category ' + (category.name || 'New category'));
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) selectedCategories.add(category.key); else selectedCategories.delete(category.key);
        updateCategorySelection();
      });
      const link = button('', 'category-list-button', () => chooseCategory(category.key));
      link.setAttribute('aria-current', category.key === activeKey ? 'true' : 'false');
      link.append(element('span', 'category-list-title', category.name || 'Untitled category'));
      const subCount = category.subcategories.filter(s => !s.removed).length;
      const state = category.removed ? ' · Removal pending' : category.id == null ? ' · New' : categoryChanged(category) ? ' · Edited' : '';
      link.append(element('span', 'category-list-meta', (category.slug ? category.slug + ' · ' : '') + subCount + ' subcategories · ' + category.posts + ' posts' + state));
      row.append(checkbox, link);
      list.appendChild(row);
    });
    if (!visible.length) list.append(element('p', 'small text-muted p-3 mb-0', 'No matching categories.'));
    list.scrollTop = scroll;
    updateCategorySelection();
  }
  function updateCategorySelection() {
    const visible = visibleCategories().filter(c => !c.removed);
    const checked = visible.filter(c => selectedCategories.has(c.key)).length;
    selectCategories.checked = visible.length > 0 && checked === visible.length;
    selectCategories.indeterminate = checked > 0 && checked < visible.length;
    selectCategories.disabled = !visible.length;
    const count = categories.filter(c => !c.removed && selectedCategories.has(c.key)).length;
    removeCategories.textContent = count ? 'Remove selected (' + count + ')' : 'Remove selected';
    removeCategories.disabled = !count;
  }
  function inputFor(item, field, maxLength, isCategory, disabled) {
    const input = element('input', 'form-control form-control-sm');
    input.type = 'text';
    input.value = item[field];
    input.maxLength = maxLength;
    input.disabled = disabled;
    input.required = field === 'name';
    input.setAttribute('aria-label', (isCategory ? 'Category ' : 'Subcategory ') + field + (isCategory ? '' : ' for ' + (item.name || 'new row')));
    input.dataset.rowKey = item.key;
    input.dataset.field = field;
    if (isCategory && field === 'name') input.dataset.categoryName = 'true';
    if (field === 'slug') input.placeholder = item.id == null ? 'Auto from name if blank' : 'Keep saved slug if blank';
    input.addEventListener('input', () => {
      item[field] = input.value;
      if (isCategory && field === 'name') document.getElementById('category-detail-heading').textContent = item.name || 'New category';
      message();
      renderList();
      updateChangeBar();
    });
    return input;
  }
  function renderDetail(keepScroll = true) {
    const scroll = keepScroll ? detail.scrollTop : 0;
    detail.replaceChildren();
    const category = activeCategory();
    if (!category) {
      detail.append(element('h2', 'h5', 'Choose a category'));
      detail.append(element('p', 'text-muted', 'Select a category on the left, or create a new one.'));
      return;
    }
    const heading = element('div', 'd-flex flex-wrap justify-content-between align-items-center gap-2 mb-3');
    const title = element('h2', 'h5 mb-0', category.name || 'New category');
    title.id = 'category-detail-heading';
    const remove = button(category.removed ? 'Undo category removal' : 'Remove category', 'btn btn-sm ' + (category.removed ? 'btn-outline-light' : 'btn-outline-danger'), () => {
      category.removed = !category.removed;
      selectedCategories.delete(category.key);
      message();
      renderList(); renderDetail(); updateChangeBar();
    });
    heading.append(title, remove);
    detail.appendChild(heading);
    if (category.removed) detail.append(element('div', 'alert alert-warning small', 'Removal pending. This category and its subcategories will be removed when you save. Undo to keep editing.'));
    const row = element('div', 'row g-3 mb-3');
    for (const [field, text, limit] of [['name', 'Display name', 100], ['slug', 'Slug', 50]]) {
      const label = element('label', 'col-md-6 small');
      label.append(element('span', 'd-block mb-1', text), inputFor(category, field, limit, true, Boolean(category.removed)));
      row.appendChild(label);
    }
    detail.append(row, element('p', 'small text-muted', category.posts + ' posts · ' + category.drafts + ' drafts · ' + (category.id == null ? 'New category' : 'Category #' + category.id)));
    const controls = element('div', 'd-flex flex-wrap gap-2 align-items-center mb-2');
    const subSearch = element('input', 'form-control form-control-sm');
    subSearch.type = 'search';
    subSearch.placeholder = 'Find a subcategory…';
    subSearch.setAttribute('aria-label', 'Filter subcategories');
    subSearch.value = subSearches.get(category.key) || '';
    subSearch.disabled = Boolean(category.removed);
    subSearch.style.maxWidth = '240px';
    subSearch.addEventListener('input', () => { subSearches.set(category.key, subSearch.value); renderSubRows(); });
    const add = button('+ Add subcategory', 'btn btn-sm btn-outline-primary', () => {
      const sub = {id: null, key: 's-new-' + (++counter), name: '', slug: '', posts: 0, drafts: 0};
      category.subcategories.push(sub);
      subSearches.set(category.key, '');
      message(); renderDetail(); renderList(); updateChangeBar();
      detail.querySelector('[data-row-key="' + sub.key + '"][data-field="name"]')?.focus();
    });
    add.disabled = Boolean(category.removed);
    const removeSubs = button('Remove selected', 'btn btn-sm btn-outline-danger', () => {
      category.subcategories.forEach(sub => {
        if (selectedSubs.has(sub.key)) { sub.removed = true; selectedSubs.delete(sub.key); }
      });
      message(); renderSubRows(); renderList(); updateChangeBar();
    });
    removeSubs.id = 'remove-selected-subs';
    controls.append(subSearch, add, removeSubs);
    detail.appendChild(controls);
    const responsive = element('div', 'table-responsive');
    const table = element('table', 'table table-dark table-sm category-sub-table');
    table.innerHTML = '<thead><tr><th><input type="checkbox" class="form-check-input" id="select-visible-subs" aria-label="Select visible subcategories"></th><th>Name</th><th>Slug</th><th>Posts</th><th>Drafts</th><th></th></tr></thead><tbody id="category-sub-rows"></tbody>';
    table.querySelector('#select-visible-subs').addEventListener('change', event => {
      filteredSubs().filter(s => !s.removed).forEach(sub => {
        if (event.target.checked) selectedSubs.add(sub.key); else selectedSubs.delete(sub.key);
      });
      renderSubRows();
    });
    responsive.appendChild(table);
    detail.appendChild(responsive);
    detail.append(element('p', 'small text-muted mb-0', 'Removed rows stay visible so you can undo them before saving. Slugs must be unique within their category.'));
    renderSubRows();
    detail.scrollTop = scroll;
  }
  function filteredSubs() {
    const category = activeCategory();
    const term = (subSearches.get(category.key) || '').trim().toLowerCase();
    return category.subcategories.filter(sub => matches(sub, term));
  }
  function renderSubRows() {
    const category = activeCategory();
    const body = document.getElementById('category-sub-rows');
    if (!body || !category) return;
    body.replaceChildren();
    const rows = filteredSubs();
    rows.forEach(sub => {
      const tr = element('tr', sub.removed ? 'category-sub-removed' : '');
      const choose = element('input', 'form-check-input');
      choose.type = 'checkbox';
      choose.checked = selectedSubs.has(sub.key);
      choose.disabled = Boolean(sub.removed || category.removed);
      choose.setAttribute('aria-label', 'Select subcategory ' + (sub.name || 'new row'));
      choose.addEventListener('change', () => {
        if (choose.checked) selectedSubs.add(sub.key); else selectedSubs.delete(sub.key);
        updateSubSelection();
      });
      const selection = element('td'); selection.appendChild(choose);
      const name = element('td', 'category-name-cell');
      name.appendChild(inputFor(sub, 'name', 120, false, Boolean(sub.removed || category.removed)));
      const slug = element('td', 'category-slug-cell');
      slug.appendChild(inputFor(sub, 'slug', 60, false, Boolean(sub.removed || category.removed)));
      const actions = element('td');
      if (sub.removed) {
        const undo = button('Undo', 'btn btn-sm btn-outline-light', () => {
          sub.removed = false; message(); renderSubRows(); renderList(); updateChangeBar();
        });
        undo.disabled = Boolean(category.removed);
        actions.appendChild(undo);
      } else actions.append(element('span', 'small text-muted', sub.id == null ? 'New' : '#' + sub.id));
      tr.append(selection, name, slug, element('td', 'small', sub.posts), element('td', 'small', sub.drafts), actions);
      body.appendChild(tr);
    });
    if (!rows.length) {
      const tr = element('tr');
      const td = element('td', 'text-muted py-3', category.subcategories.length ? 'No matching subcategories.' : 'No subcategories yet. Add a row above.');
      td.colSpan = 6; tr.appendChild(td); body.appendChild(tr);
    }
    updateSubSelection();
  }
  function updateSubSelection() {
    const category = activeCategory();
    if (!category) return;
    const visible = filteredSubs().filter(s => !s.removed);
    const checked = visible.filter(s => selectedSubs.has(s.key)).length;
    const master = document.getElementById('select-visible-subs');
    master.checked = visible.length > 0 && checked === visible.length;
    master.indeterminate = checked > 0 && checked < visible.length;
    master.disabled = !visible.length || Boolean(category.removed);
    const count = category.subcategories.filter(s => !s.removed && selectedSubs.has(s.key)).length;
    const remove = document.getElementById('remove-selected-subs');
    remove.disabled = !count || Boolean(category.removed);
    remove.textContent = count ? 'Remove selected (' + count + ')' : 'Remove selected';
  }
  function adopt(snapshot, preferredKey = activeKey) {
    const previousIndex = categories.findIndex(c => c.key === activeKey);
    base = clone(snapshot);
    baseCategories = new Map(base.categories.map(c => [c.id, c]));
    baseSubs = new Map(base.categories.flatMap(c => c.subcategories.map(s => [s.id, s])));
    categories = clone(snapshot.categories).map(c => ({...c, key: 'c-' + c.id,
      subcategories: c.subcategories.map(s => ({...s, key: 's-' + s.id}))}));
    selectedCategories.clear(); selectedSubs.clear();
    activeKey = categories.some(c => c.key === preferredKey) ? preferredKey : categories[Math.max(0, Math.min(previousIndex, categories.length - 1))]?.key;
    rememberSelection(activeCategory());
    renderList(); renderDetail(); updateChangeBar();
  }
  function payload() {
    return {
      version: base.version,
      delete_category_ids: categories.filter(c => c.removed && c.id != null).map(c => c.id),
      delete_subcategory_ids: categories.filter(c => !c.removed).flatMap(c => c.subcategories.filter(s => s.removed && s.id != null).map(s => s.id)),
      categories: categories.filter(c => !c.removed).map(c => ({id: c.id, key: c.key, name: c.name, slug: c.slug,
        subcategories: c.subcategories.filter(s => !s.removed).map(s => ({id: s.id, key: s.key, name: s.name, slug: s.slug}))})),
    };
  }
  async function fetchData(options = {}) {
    const response = await fetch(form.dataset.url, {cache: 'no-store', ...options});
    if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('Could not load the editor response. Please try again or sign in again.');
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not save changes. Your edits are still here.');
    return result;
  }
  async function saveAll() {
    if (busy || !changes().total) return;
    for (const category of categories.filter(c => !c.removed)) {
      const invalid = [category, ...category.subcategories.filter(s => !s.removed)].find(item => !item.name.trim());
      if (invalid) {
        search.value = ''; subSearches.set(category.key, '');
        chooseCategory(category.key);
        message('Give every remaining category and subcategory a name before saving.', true);
        detail.querySelector('[data-row-key="' + invalid.key + '"][data-field="name"]')?.focus();
        return;
      }
    }
    const count = changes();
    if ((count.categories || count.subcategories) && !window.confirm('Save all changes and remove ' + count.categories + ' categories and ' + count.subcategories + ' subcategories? Jokes and drafts will be kept; affected category assignments will be cleared.')) return;
    busy = true; fields.disabled = true; updateChangeBar(); message('Saving all changes…');
    try {
      const previousKey = activeKey;
      const result = await fetchData({method: 'POST', headers: {Accept: 'application/json', 'Content-Type': 'application/json'}, body: JSON.stringify(payload())});
      const mapped = result.created_ids?.[previousKey];
      adopt(result.snapshot, mapped ? 'c-' + mapped : previousKey);
      message(result.message || 'All changes saved.');
    } catch (error) {
      message(error.message, true);
    } finally {
      busy = false; fields.disabled = false; updateChangeBar();
    }
  }
  form.addEventListener('submit', event => { event.preventDefault(); saveAll(); });
  document.getElementById('reload-categories').addEventListener('click', async () => {
    if (busy || (changes().total && !window.confirm('Discard all unsaved category edits and reload saved data?'))) return;
    busy = true; fields.disabled = true;
    try { adopt((await fetchData({headers: {Accept: 'application/json'}})).snapshot); message('Saved categories reloaded.'); }
    catch (error) { message(error.message, true); }
    finally { busy = false; fields.disabled = false; updateChangeBar(); }
  });
  document.getElementById('add-category').addEventListener('click', () => {
    const category = {id: null, key: 'c-new-' + (++counter), name: '', slug: '', posts: 0, drafts: 0, subcategories: []};
    categories.push(category); search.value = ''; message(); chooseCategory(category.key, true); updateChangeBar();
  });
  search.addEventListener('input', renderList);
  selectCategories.addEventListener('change', () => {
    visibleCategories().filter(c => !c.removed).forEach(c => {
      if (selectCategories.checked) selectedCategories.add(c.key); else selectedCategories.delete(c.key);
    });
    renderList();
  });
  removeCategories.addEventListener('click', () => {
    categories.forEach(c => { if (selectedCategories.has(c.key)) c.removed = true; });
    selectedCategories.clear(); message(); renderList(); renderDetail(); updateChangeBar();
  });
  window.addEventListener('beforeunload', event => {
    if (changes().total) { event.preventDefault(); event.returnValue = ''; }
  });
  document.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); saveAll(); }
  });
  let preferred;
  const hash = location.hash.match(/^#cat-(\d+)$/);
  try { preferred = hash?.[1] || sessionStorage.getItem('admin-category-active'); } catch (_) { preferred = hash?.[1]; }
  adopt(JSON.parse(document.getElementById('category-editor-data').textContent), preferred ? 'c-' + preferred : undefined);
})();
