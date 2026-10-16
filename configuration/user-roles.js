// user-roles.js
document.addEventListener('DOMContentLoaded', async () => {
  const tbody = document.querySelector('.roles-table tbody');
  const searchInput = document.getElementById('rolesSearch');
  const tableInfoSummary = document.getElementById('tableInfoSummary');
  const newRoleBtn = document.getElementById('newRoleBtn');
  const permissionsBtn = document.getElementById('permissionsBtn');

  let allRoles = [];

  async function loadRoles() {
    try {
      const response = await fetchAPI('/user-roles/');
      
      // Safely handle both array and paginated DRF responses
      if (Array.isArray(response)) {
        allRoles = response;
      } else if (response && Array.isArray(response.results)) {
        allRoles = response.results;
      } else {
        allRoles = [];
      }

      renderRolesTable(allRoles);
    } catch (error) {
      console.error('Failed to load user roles:', error);
      if (tbody) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: #dc3545; padding: 20px;">Failed to load roles from server.</td></tr>`;
      }
    }
  }

  function renderRolesTable(roles) {
    if (!tbody) return;
    tbody.innerHTML = '';

    if (!roles || roles.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; padding: 20px;">No user roles found.</td></tr>`;
      if (tableInfoSummary) tableInfoSummary.textContent = `Showing 0 to 0 of 0 entries`;
      return;
    }

    roles.forEach((role, index) => {
      const row = document.createElement('tr');
      row.innerHTML = `
        <td class="col-num">${index + 1}</td>
        <td class="role-name-text">${escapeHtml(role.name)}</td>
        <td class="col-center"><button class="btn-action btn-set-permissions" data-id="${role.id}">set permissions</button></td>
        <td class="col-center"><button class="btn-action btn-edit" data-id="${role.id}">edit</button></td>
        <td class="col-center"><button class="btn-action btn-delete" data-id="${role.id}">delete</button></td>
      `;
      tbody.appendChild(row);
    });

    if (tableInfoSummary) {
      tableInfoSummary.textContent = `Showing 1 to ${roles.length} of ${roles.length} entries`;
    }

    attachRoleListeners();
  }

  function attachRoleListeners() {
    document.querySelectorAll('.btn-delete').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        const id = e.target.getAttribute('data-id');
        const row = e.target.closest('tr');
        const roleName = row.querySelector('.role-name-text').textContent;

        if (confirm(`Are you sure you want to delete role: "${roleName}"?`)) {
          try {
            await fetchAPI(`/user-roles/${id}/`, { method: 'DELETE' });
            loadRoles();
          } catch (err) {
            alert('Failed to delete role: ' + (err.message || 'Server error'));
          }
        }
      });
    });

    document.querySelectorAll('.btn-set-permissions').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const id = e.target.getAttribute('data-id');
        alert(`Open permissions configuration modal for Role ID: ${id}`);
      });
    });

    document.querySelectorAll('.btn-edit').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const id = e.target.getAttribute('data-id');
        alert(`Open edit modal for Role ID: ${id}`);
      });
    });
  }

  if (newRoleBtn) {
    newRoleBtn.addEventListener('click', () => {
      const roleName = prompt('Enter new role name:');
      if (roleName && roleName.trim() !== '') {
        createNewRole(roleName.trim());
      }
    });
  }

  if (permissionsBtn) {
    permissionsBtn.addEventListener('click', () => {
      alert('Global permissions configuration view.');
    });
  }

  async function createNewRole(name) {
    try {
      await fetchAPI('/user-roles/', {
        method: 'POST',
        body: JSON.stringify({ name: name, permissions: [] })
      });
      alert('Role created successfully!');
      loadRoles();
    } catch (error) {
      console.error('Error creating role:', error);
      alert('Failed to save role. Please try again.');
    }
  }

  if (searchInput) {
    searchInput.addEventListener('input', (e) => {
      const term = e.target.value.toLowerCase();
      const filtered = allRoles.filter(role => role.name.toLowerCase().includes(term));
      renderRolesTable(filtered);
    });
  }

  function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/[&<>"']/g, function(m) {
      return {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
      }[m];
    });
  }

  loadRoles();
});