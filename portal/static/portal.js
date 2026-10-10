"use strict";
(() => {
  const $ = id => document.getElementById(id);
  let csrf = "", timer = null, currentRequest = null, searchGeneration = 0;
  const names = {requested:"Requested",searching:"Searching",waiting:"Waiting for a release",queued:"Queued",downloading:"Downloading",importing:"Importing",available:"Available in Emby",partial:"Partly available",attention:"Needs attention",failed:"Failed"};
  function message(text="") { $("message").textContent = text; }
  async function api(path, options={}) {
    const res = await fetch(path, {credentials:"same-origin", ...options, headers:{"Accept":"application/json", ...(options.body ? {"Content-Type":"application/json", "X-CSRF-Token":csrf} : {}), ...options.headers}});
    let body;
    try { body = await res.json(); } catch { throw new Error("The service is temporarily unavailable. Please try again."); }
    if (!res.ok) {
      if (res.status === 401 && path !== "/api/login") signedOut();
      throw new Error(typeof body.detail === "string" ? body.detail : "Please try again.");
    }
    return body;
  }
  function signedOut() {
    clearInterval(timer); timer = null; csrf = ""; currentRequest = null;
    $("login-section").hidden = false; $("search-section").hidden = true; $("account").hidden = true;
    $("status").replaceChildren(); $("results").replaceChildren(); $("history-list").replaceChildren();
  }
  async function signedIn(user) {
    csrf = user.csrf; $("username").textContent = user.name;
    $("login-section").hidden = true; $("search-section").hidden = false; $("account").hidden = false;
    $("login-password").value = ""; message(); $("query").focus();
    clearInterval(timer);
    timer = setInterval(() => refreshRequests().catch(e => message(e.message)), 15000);
    await refreshRequests();
  }
  function requestElement(item) {
    const box = document.createElement("article"); box.className = "request";
    const title = document.createElement("h2"); title.textContent = item.title + (item.year ? " (" + item.year + ")" : ""); box.append(title);
    const line = document.createElement("div"); line.className = "status-line";
    const state = document.createElement("span"); state.className = "state " + item.state; state.textContent = names[item.state] || "Updating";
    const detail = document.createElement("span"); detail.className = "detail"; detail.textContent = item.detail;
    line.append(state, detail); box.append(line);
    if (typeof item.progress === "number" && item.state === "downloading") { const p=document.createElement("progress"); p.max=100; p.value=item.progress; p.setAttribute("aria-label","Download progress"); box.append(p); }
    if (item.emby_url && /^https?:\/\//.test(item.emby_url)) { const link=document.createElement("a");link.href=item.emby_url;link.textContent="Open in Emby →";link.target="_blank";link.rel="noopener noreferrer";box.append(link); }
    return box;
  }
  async function refreshRequests() {
    const data = await api("/api/requests");
    if (!data.requests.length) { $("status").replaceChildren(); $("history").hidden=true; return; }
    const active = data.requests.find(x => x.id === currentRequest) || data.requests[0];
    $("status").replaceChildren(requestElement(active));
    const earlier = data.requests.filter(x => x.id !== active.id);
    $("history").hidden = !earlier.length; $("history-list").replaceChildren(...earlier.map(requestElement));
  }
  $("login-form").addEventListener("submit", async e => {
    e.preventDefault(); const button=e.currentTarget.querySelector("button");button.disabled=true;message();
    try { const user=await api("/api/login",{method:"POST",body:JSON.stringify({username:$("login-name").value.trim(),password:$("login-password").value})}); await signedIn(user); }
    catch(e){message(e.message);}finally{button.disabled=false;}
  });
  $("logout").addEventListener("click", async () => {try{await api("/api/logout",{method:"POST",body:"{}"});signedOut();message();}catch(e){message(e.message);}});
  $("search-form").addEventListener("submit", async e => {
    e.preventDefault(); const generation=++searchGeneration, kind=document.querySelector("input[name=kind]:checked").value, query=$("query").value.trim();
    if(!query)return; message(); $("busy").hidden=false; $("results").replaceChildren();
    try {
      const data=await api("/api/search?"+new URLSearchParams({kind,q:query}));if(generation!==searchGeneration)return;
      if(!data.results.length){const p=document.createElement("p");p.className="hint";p.textContent="No matching titles. Try another spelling or media type.";$("results").append(p);}
      data.results.forEach(item => {
        const row=document.createElement("article");row.className="result";
        const copy=document.createElement("div");copy.className="copy";
        const title=document.createElement("h2");title.textContent=item.title;
        if(item.year){const year=document.createElement("span");year.className="year";year.textContent=" ("+item.year+")";title.append(year);}
        const desc=document.createElement("p");desc.textContent=item.overview;copy.append(title,desc);
        const button=document.createElement("button");button.textContent="Add to Emby";
        button.addEventListener("click",async()=>{
          for(const b of $("results").querySelectorAll("button"))b.disabled=true;
          button.textContent="Adding…";message();
          try{const request=await api("/api/requests",{method:"POST",body:JSON.stringify({kind,provider_id:item.provider_id})});currentRequest=request.id;$("results").replaceChildren();$("query").value="";$("status").replaceChildren(requestElement(request));$("query").focus();await refreshRequests();}
          catch(e){message(e.message);button.textContent="Add to Emby";for(const b of $("results").querySelectorAll("button"))b.disabled=false;}
        });row.append(copy,button);$("results").append(row);
      });
    }catch(e){message(e.message);}finally{if(generation===searchGeneration)$("busy").hidden=true;}
  });
  api("/api/me").then(signedIn).catch(e=>{signedOut();if(e.message!=="Sign in to continue")message(e.message);});
})();
