// users.js
document.addEventListener('DOMContentLoaded', async () => {
  const usersGrid = document.querySelector('.users-grid');
  const modal = document.getElementById('createUserModal');
  const openAddUserBtn = document.getElementById('openAddUserModal');
  const closeUserModalBtn = document.getElementById('closeUserModal');
  const cancelUserModalBtn = document.getElementById('cancelUserModal');
  const createUserForm = document.getElementById('createUserForm');

  // Open Modal logic
  if (openAddUserBtn && modal) {
    openAddUserBtn.addEventListener('click', () => {
      modal.style.display = 'flex';
    });
  }

  // Close Modal logic
  function closeModal() {
    if (modal) {
      modal.style.display = 'none';
      if (createUserForm) createUserForm.reset();
    }
  }

  if (closeUserModalBtn) closeUserModalBtn.addEventListener('click', closeModal);
  if (cancelUserModalBtn) cancelUserModalBtn.addEventListener('click', closeModal);

  window.addEventListener('click', (e) => {
    if (e.target === modal) {
      closeModal();
    }
  });

  // Handle Form Submission
  if (createUserForm) {
    createUserForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      
      const payload = {
        name: document.getElementById('fullName').value,
        username: document.getElementById('username').value,
        email: document.getElementById('emailAddress').value,
        phone: document.getElementById('phone').value,
        position: document.getElementById('position').value,
        role: document.getElementById('userRoles').value,
        password: document.getElementById('password').value
      };

      try {
        await fetchAPI('/user-roles/', {
          method: 'POST',
          body: JSON.stringify(payload)
        });

        alert('User created successfully!');
        closeModal();
        loadUsers();
      } catch (error) {
        console.error('Failed to create user:', error);
        alert('Error saving user. Please check your inputs.');
      }
    });
  }

  async function loadUsers() {
    try {
      const response = await fetchAPI('/user-roles/');
      
      // Handle Django DRF pagination wrapper ({ results: [...] }) vs direct array ([...])
      const users = Array.isArray(response) ? response : (response.results || []);
      
      renderUserCards(users);
    } catch (error) {
      console.error('Failed to load users:', error);
      if (usersGrid) {
        usersGrid.innerHTML = `<p style="padding: 20px; color: #e53e3e;">Failed to load user profiles from server.</p>`;
      }
    }
  }

  function renderUserCards(users) {
    if (!usersGrid) return;
    usersGrid.innerHTML = '';

    if (!users || users.length === 0) {
      usersGrid.innerHTML = `<p style="padding: 20px;">No user profiles available.</p>`;
      return;
    }

    users.forEach(user => {
      const card = document.createElement('div');
      card.className = 'user-card';

      // Format role name for CSS class (e.g., "Super Admin" -> "super-admin")
      const roleText = user.role || 'super admin';
      const roleClass = roleText.toLowerCase().replace(/\s+/g, '-');
      
      card.innerHTML = `
        <div class="avatar-circle">
          <i class="fa-solid fa-user"></i>
        </div>
        <h3 class="user-name-title">${user.name || user.username || 'System User'}</h3>
        <p class="user-meta">Username: <span class="meta-val">${user.username || 'N/A'}</span></p>
        <p class="user-meta email-meta">${user.email || 'N/A'}</p>
        <p class="user-meta">Phone: <span class="meta-val">${user.phone || 'N/A'}</span></p>
        <p class="user-meta role-meta">Role(s): <span class="role-badge ${roleClass}">${roleText}</span></p>
        
        <div class="card-actions">
          <div class="status-badge active-status">User is active</div>
          <button class="btn-view-details" data-id="${user.id || ''}">View details</button>
        </div>
      `;
      usersGrid.appendChild(card);
    });

    attachUserListeners();
  }

  function attachUserListeners() {
    const detailButtons = document.querySelectorAll('.btn-view-details');
    detailButtons.forEach((btn) => {
      btn.addEventListener('click', (e) => {
        const userId = e.target.getAttribute('data-id');
        const card = e.target.closest('.user-card');
        const userName = card.querySelector('.user-name-title').textContent;
        alert(`Opening details for user: ${userName} (ID: ${userId})`);
      });
    });
  }

  loadUsers();
});