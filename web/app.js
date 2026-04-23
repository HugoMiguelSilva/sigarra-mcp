const messagesEl = document.getElementById("messages");
const template = document.getElementById("message-template");
const chatForm = document.getElementById("chat-form");
const loginForm = document.getElementById("login-form");
const logoutBtn = document.getElementById("logout-btn");
const newChatBtn = document.getElementById("new-chat");
const messageInput = document.getElementById("message-input");
const sendBtn = document.getElementById("send-btn");
const sessionStatusEl = document.getElementById("session-status");
const conversationListEl = document.getElementById("conversation-list");
const loginErrorEl = document.getElementById("login-error");

let currentConversationId = null;

// Add event listener for Enter key on message input
messageInput.addEventListener('keydown', function(event) {
  if (event.key === 'Enter'  && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  }
});

function clearMessages() {
  messagesEl.innerHTML = "";
}

function appendMessage(role, text, sourceUrl = null) {
  const node = template.content.firstElementChild.cloneNode(true);
  if (role === "user") node.classList.add("user");

  const bubble = node.querySelector(".msg-bubble");
  bubble.textContent = text;

  if (sourceUrl) {
    const link = document.createElement("a");
    link.href = sourceUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.className = "source-link";
    link.textContent = "Abrir fonte no SIGARRA";
    bubble.appendChild(document.createElement("br"));
    bubble.appendChild(link);
  }

  messagesEl.appendChild(node);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function renderConversations(conversations) {
  conversationListEl.innerHTML = "";
  if (!conversations.length) {
    const empty = document.createElement("p");
    empty.textContent = "Sem conversas guardadas.";
    conversationListEl.appendChild(empty);
    return;
  }

  conversations.forEach((conversation) => {
    const row = document.createElement("div");
    row.className = "conversation-row";

    const item = document.createElement("button");
    item.type = "button";
    item.className = "conversation-item";
    if (conversation.id === currentConversationId) {
      item.classList.add("active");
    }
    item.textContent = conversation.title;
    item.addEventListener("click", () => {
      loadConversation(conversation.id);
    });

    const renameBtn = document.createElement("button");
    renameBtn.type = "button";
    renameBtn.className = "conversation-rename-btn";
    renameBtn.textContent = "Editar";
    renameBtn.title = "Alterar titulo da conversa";
    renameBtn.addEventListener("click", async (event) => {
      event.stopPropagation();
      await renameConversation(conversation.id, conversation.title);
    });

    row.appendChild(item);
    row.appendChild(renameBtn);
    conversationListEl.appendChild(row);
  });
}

async function renameConversation(conversationId, currentTitle) {
  const proposedTitle = window.prompt("Novo titulo da conversa:", currentTitle || "");
  if (proposedTitle === null) return;

  const title = proposedTitle.trim();
  if (!title) {
    appendMessage("assistant", "O titulo nao pode ser vazio.");
    return;
  }

  try {
    const response = await fetch(`/api/conversations/${conversationId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    let data = {};
    try {
      data = await response.json();
    } catch (_) {
      data = {};
    }

    if (!response.ok) {
      if (response.status === 404 && data.detail === "Not Found") {
        appendMessage(
          "assistant",
          "O servidor ainda nao tem a rota de edicao. Reinicia o web_app.py e atualiza a pagina (Ctrl+F5)."
        );
        return;
      }
      appendMessage("assistant", data.detail || "Erro ao alterar o titulo.");
      return;
    }

    await refreshConversations();
  } catch (_) {
    appendMessage("assistant", "Erro de rede ao alterar o titulo.");
  }
}

async function refreshConversations() {
  try {
    const response = await fetch("/api/conversations", { cache: "no-store" });
    const data = await response.json();
    const conversations = data.conversations || [];

    if (currentConversationId === null && conversations.length) {
      currentConversationId = conversations[0].id;
      await loadConversation(currentConversationId, false);
    }

    renderConversations(conversations);
  } catch (_) {
    conversationListEl.innerHTML = "<p>Erro ao carregar historico.</p>";
  }
}

async function createConversation(title = "Nova conversa") {
  const response = await fetch("/api/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!response.ok) {
    throw new Error("Falha ao criar conversa");
  }
  const data = await response.json();
  currentConversationId = data.id;
  await refreshConversations();
  clearMessages();
  appendMessage("assistant", "Nova conversa iniciada. Como posso ajudar?");
}

async function loadConversation(conversationId, updateList = true) {
  try {
    currentConversationId = conversationId;
    const response = await fetch(`/api/conversations/${conversationId}/messages`, { cache: "no-store" });
    const data = await response.json();
    clearMessages();

    (data.messages || []).forEach((msg) => {
      appendMessage(msg.role, msg.text, msg.source_url || null);
    });

    if (!data.messages || !data.messages.length) {
      appendMessage("assistant", "Conversa vazia. Escreve a tua primeira pergunta.");
    }

    if (updateList) {
      await refreshConversations();
    } else {
      const listResponse = await fetch("/api/conversations", { cache: "no-store" });
      const listData = await listResponse.json();
      renderConversations(listData.conversations || []);
    }
  } catch (_) {
    appendMessage("assistant", "Erro ao abrir a conversa selecionada.");
  }
}

function setSending(isSending) {
  sendBtn.disabled = isSending;
  sendBtn.textContent = isSending ? "A enviar..." : "Enviar";
}

async function sendMessage() {
  const message = messageInput.value.trim();
  if (!message) return;

  appendMessage("user", message);
  messageInput.value = "";
  setSending(true);

  try {
    if (currentConversationId === null) {
      await createConversation();
    }

    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, conversation_id: currentConversationId }),
    });

    const data = await response.json();
    if (!response.ok) {
      appendMessage("assistant", data.detail || "Erro ao processar a pergunta.");
      return;
    }

    if (data.conversation_id) {
      currentConversationId = data.conversation_id;
    }

    appendMessage("assistant", data.answer || "Sem resposta.", data.source_url || null);
    await refreshConversations();
  } catch (_) {
    appendMessage("assistant", "Erro de rede ao comunicar com o servidor.");
  } finally {
    setSending(false);
  }
}

async function refreshSessionStatus() {
  try {
    const response = await fetch("/api/session", { cache: "no-store" });
    const data = await response.json();
    sessionStatusEl.textContent = data.status || "Sem informacao de sessao.";
  } catch (_) {
    sessionStatusEl.textContent = "Nao foi possivel obter o estado da sessao.";
  }
}

async function refreshUiAfterAuthChange() {
  currentConversationId = null;
  clearMessages();
  await refreshConversations();

  if (currentConversationId === null) {
    appendMessage(
      "assistant",
      "Sessao atualizada. Escolhe uma conversa ou cria uma nova."
    );
  }
}

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await sendMessage();
});

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  // Clear previous error
  loginErrorEl.textContent = "";
  loginErrorEl.style.display = "none";

  const username = document.getElementById("username").value.trim();
  const password = document.getElementById("password").value;

  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });

    const data = await response.json();
    if (data.ok) {
      appendMessage("assistant", data.message || "Login realizado com sucesso.");
      await refreshUiAfterAuthChange();
    } else {
      loginErrorEl.textContent = data.message || "Erro desconhecido.";
      loginErrorEl.style.display = "block";
    }
    await refreshSessionStatus();
  } catch (_) {
    loginErrorEl.textContent = "Erro ao efetuar login.";
    loginErrorEl.style.display = "block";
  }
});

logoutBtn.addEventListener("click", async () => {
  try {
    const response = await fetch("/api/logout", { method: "POST" });
    const data = await response.json();
    appendMessage("assistant", data.message || "Sessao terminada.");
    await refreshUiAfterAuthChange();
    await refreshSessionStatus();
  } catch (_) {
    appendMessage("assistant", "Erro ao terminar sessao.");
  }
});

newChatBtn.addEventListener("click", () => {
  createConversation().catch(() => {
    appendMessage("assistant", "Erro ao criar nova conversa.");
  });
});

async function bootstrap() {
  await refreshSessionStatus();
  await refreshConversations();

  if (currentConversationId === null) {
    clearMessages();
    appendMessage(
      "assistant",
      "Assistente pronto. Cria uma conversa e pergunta sobre horario, exames, UCs, docentes, cantina ou estacionamento."
    );
  }
}

bootstrap();
