const DEFAULT_API_BASE = "https://clipforge-ai-api.onrender.com";
let API=(localStorage.getItem("clipforgeApiBase")||DEFAULT_API_BASE).replace(/\/$/,"");
const $=id=>document.getElementById(id);
async function api(path,opts={}){
  const headers=new Headers(opts.headers||{});
  const token=localStorage.getItem("clipforgeAccessToken")||"";
  if(token)headers.set("Authorization",`Bearer ${token}`);
  let r;
  try{r=await fetch(API+path,{...opts,headers})}
  catch(err){throw new Error(`Cannot reach ${API}. Check your internet connection, Render deployment, and backend URL in Settings.`)}
  if(!r.ok){
    let d={};try{d=await r.json()}catch{}
    const detail=typeof d.detail==="string"?d.detail:`Backend error ${r.status}`;
    throw new Error(detail);
  }
  return r.json();
}
function escapeHtml(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
async function refresh(){try{const d=await api("/api/dashboard");$("apiStatus").textContent="● Backend online";$("apiStatus").style.color="#54e69b";$("clipCount").textContent=d.clips_created;$("jobCount").textContent=d.jobs.filter(j=>["queued","transcribing","rendering"].includes(j.status)).length;$("jobs").innerHTML=d.jobs.length?d.jobs.slice(0,8).map(j=>`<div class="job"><div><b>${escapeHtml(j.source_name)}</b><br><small>${escapeHtml(j.message||"")} · ${escapeHtml(j.created_at)}</small></div><span class="status">${escapeHtml(j.status)}</span></div>`).join(""):'<p class="empty">No jobs yet. Upload a video to get started.</p>';
    const clips=d.clips||[];
    $("clipList").innerHTML=clips.length?clips.slice(0,12).map(c=>`<div class="job"><div><b>${escapeHtml(c.title||"ClipForge Short")}</b><br><small>${escapeHtml(c.duration)} sec · ${escapeHtml(c.filename)}</small></div><div class="clip-actions"><button class="link" data-youtube-upload="${escapeHtml(c.id)}">Upload to YouTube</button></div></div>`).join(""):'<p class="empty">No clips created yet.</p>';
    $("clipList").querySelectorAll("[data-youtube-upload]").forEach(b=>b.onclick=async()=>{if(!confirm("Upload this clip to your connected YouTube channel as Private?"))return;b.disabled=true;try{const result=await api(`/api/youtube/upload/${b.dataset.youtubeUpload}`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({privacy_status:"private"})});b.textContent="Uploaded";const a=document.createElement("a");a.href=result.url;a.target="_blank";a.rel="noreferrer";a.textContent="Open YouTube video";a.className="textlink";b.parentElement.appendChild(a)}catch(err){alert(err.message);b.disabled=false}});
  }catch(e){$("apiStatus").textContent="● Backend offline";$("apiStatus").style.color="#ff8a8a";$("jobs").textContent="Connection issue: "+e.message}}
$("videoFile").addEventListener("change",e=>$("fileName").textContent=e.target.files[0]?.name||"MP4, MOV, MKV — max 2 GB");
$("uploadForm").addEventListener("submit",async e=>{e.preventDefault();const f=$("videoFile").files[0];if(!f)return;const msg=$("uploadMessage");msg.textContent="Uploading source video…";const fd=new FormData();fd.append("file",f);fd.append("clip_count",$("clipAmount").value);fd.append("clip_length",$("clipLength").value);try{const d=await api("/api/jobs",{method:"POST",body:fd});msg.textContent=`Job queued. ${d.message}`;refresh();setTimeout(refresh,5000);setTimeout(refresh,15000)}catch(err){msg.textContent=err.message}});
$("refresh").onclick=refresh;
async function refreshYouTube(){
  try{
    const d=await api("/api/youtube/connection");
    const status=$("youtubeStatus");
    if(d.connected){status.textContent="● CONNECTED";status.className="tag";$("youtubeChannel").textContent=`Connected channel: ${d.channel?.title||"YouTube channel"} ${d.channel?.custom_url||""}`;}
    else{status.textContent=d.configured?"READY TO CONNECT":"SETUP REQUIRED";status.className="tag muted";$("youtubeChannel").textContent=d.message||"Add Google OAuth credentials and the exact callback URL to your backend environment.";}
  }catch(e){$("youtubeStatus").textContent="CONNECTION ERROR";$("youtubeStatus").className="tag muted";$("youtubeChannel").textContent=e.message}
}
$("youtubeConnect").onclick=async()=>{
  const b=$("youtubeConnect");b.disabled=true;$("youtubeMessage").textContent="Preparing secure Google authorization…";
  try{const d=await api("/api/youtube/connect",{method:"POST"});if(!d.authorization_url)throw new Error("Backend did not return a Google authorization URL.");window.open(d.authorization_url,"_blank","noopener");$("youtubeMessage").textContent="Google authorization opened in a new tab. Approve ClipForge AI, then return here.";let n=0;const timer=setInterval(async()=>{n++;await refreshYouTube();const s=$("youtubeStatus").textContent;if(s.includes("CONNECTED")||n>=60)clearInterval(timer)},3000)}
  catch(e){$("youtubeMessage").textContent=e.message}
  finally{b.disabled=false}
};
$("youtubeDisconnect").onclick=async()=>{try{await api("/api/youtube/connect",{method:"DELETE"});$("youtubeMessage").textContent="YouTube disconnected.";refreshYouTube()}catch(e){$("youtubeMessage").textContent=e.message}};
$("backendUrl").value=API;
$("accessToken").value=localStorage.getItem("clipforgeAccessToken")||"";
$("saveAccessToken").onclick=()=>{localStorage.setItem("clipforgeAccessToken",$("accessToken").value.trim());location.reload()};
$("saveBackendUrl").onclick=()=>{const value=$("backendUrl").value.trim().replace(/\/$/,"");if(!/^https?:\/\//i.test(value)){ $("youtubeMessage").textContent="Enter a valid backend URL, including https://";return;}localStorage.setItem("clipforgeApiBase",value);location.reload()};

const hfTerminal=new Set(["completed","failed","nsfw","canceled"]);
function hfMessage(text,kind=""){const el=$("higgsMessage");if(el){el.textContent=text;el.className="message "+kind}}
async function refreshHiggs(){
  try{
    const c=await api("/api/higgsfield/connection");
    $("higgsStatus").textContent=c.configured?"● CONNECTED":"NOT CONNECTED";
    $("higgsStatus").className=c.configured?"tag":"tag muted";
    const history=await api("/api/higgsfield/requests");
    $("higgsHistory").innerHTML=history.requests.length?history.requests.map(r=>{
      const active=!hfTerminal.has(String(r.status||"").toLowerCase());
      const link=r.result_url?`<a class="textlink" href="${escapeHtml(r.result_url)}" target="_blank" rel="noreferrer">Open result ↗</a>`:"";
      const cancel=active?`<button class="link" data-hf-cancel="${escapeHtml(r.id)}">Cancel</button>`:"";
      return `<div class="job"><div><b>${escapeHtml(r.operation==="enhance"?"Enhanced video":"Generated video")} · ${escapeHtml(r.model)}</b><br><small>${escapeHtml(r.status)} · ${escapeHtml(r.error||r.prompt||"")}</small><br>${link}</div><div>${cancel}</div></div>`;
    }).join(""):'<p class="empty">No Higgsfield requests yet.</p>';
    $("higgsHistory").querySelectorAll("[data-hf-cancel]").forEach(b=>b.onclick=async()=>{try{await api(`/api/higgsfield/requests/${b.dataset.hfCancel}/cancel`,{method:"POST"});hfMessage("Cancellation requested.");refreshHiggs()}catch(e){hfMessage(e.message,"error")}});
  }catch(e){$("higgsStatus").textContent="CONNECTION ERROR";$("higgsStatus").className="tag muted";hfMessage(e.message,"error")}
}
$("higgsConnect").onclick=async()=>{
  const key=$("higgsApiKey").value.trim();
  if(!key){hfMessage("Paste your complete API credential first.","error");return}
  $("higgsConnect").disabled=true;hfMessage("Verifying and securely saving your Higgsfield credential…");
  try{const r=await api("/api/higgsfield/connect",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({api_key:key})});$("higgsApiKey").value="";hfMessage(r.message,"success");await refreshHiggs()}
  catch(e){hfMessage(e.message,"error")}finally{$("higgsConnect").disabled=false}
};
$("higgsDisconnect").onclick=async()=>{try{const r=await api("/api/higgsfield/connect",{method:"DELETE"});hfMessage(r.message,"success");refreshHiggs()}catch(e){hfMessage(e.message,"error")}};
$("higgsRefresh").onclick=refreshHiggs;
$("higgsGenerateForm").addEventListener("submit",async e=>{
  e.preventDefault();const b=e.submitter;b.disabled=true;hfMessage("Submitting video generation request…");
  try{
    const d=await api("/api/higgsfield/generate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({
      model:$("higgsModel").value,prompt:$("higgsPrompt").value,duration:Number($("higgsDuration").value),
      resolution:$("higgsResolution").value,aspect_ratio:$("higgsRatio").value
    })});
    hfMessage(`Submitted to Higgsfield. Request ID: ${d.request_id}`,"success");$("higgsPrompt").value="";await refreshHiggs();pollHiggs(d.id);
  }catch(err){hfMessage(err.message,"error")}finally{b.disabled=false}
});
$("higgsEnhanceForm").addEventListener("submit",async e=>{
  e.preventDefault();const b=e.submitter;const file=$("higgsVideo").files[0];if(!file)return;
  b.disabled=true;hfMessage("Uploading your video to Higgsfield and submitting enhancement…");
  const fd=new FormData();fd.append("file",file);fd.append("prompt",$("higgsEnhancePrompt").value);fd.append("mode",$("higgsMode").value);
  try{
    const d=await api("/api/higgsfield/enhance",{method:"POST",body:fd});
    hfMessage(`Enhancement submitted. Request ID: ${d.request_id}`,"success");$("higgsEnhancePrompt").value="";await refreshHiggs();pollHiggs(d.id);
  }catch(err){hfMessage(err.message,"error")}finally{b.disabled=false}
});
async function pollHiggs(id){
  let tries=0;
  const tick=async()=>{
    tries++;
    try{
      const d=await api(`/api/higgsfield/requests/${id}`);
      await refreshHiggs();
      if(hfTerminal.has(String(d.status||"").toLowerCase())){
        if(d.status==="completed")hfMessage("Higgsfield generation completed. Open the result from generation history.","success");
        else hfMessage(`Higgsfield request ${d.status}: ${d.error||"No additional details."}`,d.status==="failed"?"error":"");
        return;
      }
      if(tries>=120){hfMessage("Generation is still processing. Use Refresh to check its status.");return}
      setTimeout(tick,5000);
    }catch(err){if(tries<5)setTimeout(tick,8000);else hfMessage(err.message,"error")}
  };
  setTimeout(tick,3000);
}
refresh();
refreshYouTube();
refreshHiggs().then(async()=>{
  try{
    const h=await api("/api/higgsfield/requests");
    h.requests.filter(r=>!hfTerminal.has(String(r.status||"").toLowerCase())).forEach(r=>pollHiggs(r.id));
  }catch{}
});

async function checkConfiguration(){
  const target=$("configStatus");
  if(!target)return;
  target.textContent="Checking backend configuration…";
  try{
    const d=await api("/api/config-status");
    const checks=[
      ["Backend encryption",d.encryption_ready],
      ["Google OAuth environment variables",d.google_oauth_ready],
      ["Google Client ID format",d.google_client_id_format_ok],
      ["Google redirect URI",d.redirect_uri_matches_render],
      ["Higgsfield server API key",d.higgsfield_environment_key_present],
    ];
    target.textContent=checks.map(([name,ok])=>`${ok?"✓":"✗"} ${name}`).join(" | ");
  }catch(err){target.textContent=err.message}
}
$("checkConfiguration")?.addEventListener("click",checkConfiguration);
