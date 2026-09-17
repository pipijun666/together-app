'use strict';
const $ = (s, root=document) => root.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths={home:'<path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1Z"/>',mail:'<rect x="3" y="5" width="18" height="15" rx="3"/><path d="m3 7 9 7 9-7"/>',heart:'<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z"/>',plus:'<path d="M12 5v14M5 12h14"/>',link:'<path d="m10 13 4-4m-6 6-2 2a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m4 2 2-2a4 4 0 0 0-6-6l-4 4" transform="translate(3 3) scale(.85)"/>',user:'<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>',lock:'<rect x="5" y="10" width="14" height="11" rx="3"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2"/>',chat:'<path d="M21 11a8 8 0 0 1-8 8H8l-5 3 1-6a8 8 0 1 1 17-5Z"/>',arrow:'<path d="M5 12h14m-6-6 6 6-6 6"/>',spark:'<path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3Z"/>'};
const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name]||paths.heart}</svg>`;
const art = '<div class="melon-art" aria-hidden="true"><div class="slice"></div><i class="seed"></i><i class="seed"></i><i class="seed"></i><span class="spark">✧</span><span class="heart">♡</span></div>';
const brand = '<div class="brand"><img src="/icon.svg" alt=""><div>一起看<small>JUST BETWEEN US</small></div></div>';
let user=null, shares=[], systemState=null, currentColumn='manual', filter='all', currentId=null, timer=null, pollBusy=false, lastFocus=null;
let pendingLink='';
let refreshSeq=0; // guards against stale poll responses overwriting newer results
const pull={y:0,dx:0,dy:0,pulling:false,refreshing:false};
function isSystemShare(s){return s.source_kind==='system'||s.author==='system';}
function cleanNote(s){
  if(!s) return '';
  return s.replace(/\[hotspot:v[0-9]+:[a-f0-9]+\]\s*/g, '').replace(/^自动热点[｜|]\s*/g, '').trim();
}
const ART_THEMES = [
  { bg: 'linear-gradient(135deg, #FF6B6B 0%, #FFE66D 100%)', tag: '日落暖霞', icon: '🌅' },
  { bg: 'linear-gradient(135deg, #1CB5E0 0%, #000046 100%)', tag: '深蓝之境', icon: '🌊' },
  { bg: 'linear-gradient(135deg, #00b09b 0%, #96c93d 100%)', tag: '极光绿林', icon: '🍃' },
  { bg: 'linear-gradient(135deg, #8E2DE2 0%, #4A00E0 100%)', tag: '星海微光', icon: '✨' },
  { bg: 'linear-gradient(135deg, #F857A6 0%, #FF5858 100%)', tag: '珊瑚暖阳', icon: '🌸' },
  { bg: 'linear-gradient(135deg, #2c3e50 0%, #3498db 100%)', tag: '暮色青岩', icon: '🏔️' },
  { bg: 'linear-gradient(135deg, #F37335 0%, #FDC830 100%)', tag: '金秋晨曦', icon: '🍁' },
  { bg: 'linear-gradient(135deg, #11998e 0%, #38ef7d 100%)', tag: '清泉山风', icon: '🌿' }
];
function strHash(s){
  let h=0;for(let i=0;i<(s||'').length;i++) h=((h<<5)-h)+s.charCodeAt(i)|0;
  return Math.abs(h);
}
function generateArtCover(s, i){
  const h = strHash((s.id||'') + (s.title||''));
  const theme = ART_THEMES[h % ART_THEMES.length];
  const pName = s.metadata?.platform || (isSystemShare(s) ? '系统精选' : '精彩分享');
  const cleanTitle = (s.title||'').replace(/^\[[^\]]+\]\s*/, '').slice(0, 36);
  return `<div class="art-cover" style="background:${theme.bg};">
    <div class="art-cover-deco"></div>
    <div class="art-cover-header"><span class="art-badge">${theme.icon} ${esc(pName)}</span></div>
    <div class="art-cover-title">${esc(cleanTitle)}</div>
    <div class="art-cover-footer"><span class="art-tag">${theme.tag}</span></div>
  </div>`;
}
try { const p=new URLSearchParams(location.hash.slice(1)); pendingLink=(p.get('share')||'').slice(0,10000); if(pendingLink) history.replaceState(null,'',location.pathname); } catch {}
window.addEventListener('hashchange',()=>{try{const value=(new URLSearchParams(location.hash.slice(1)).get('share')||'').slice(0,10000);if(value){history.replaceState(null,'',location.pathname);if(user)submitSharedLink(value);else pendingLink=value;}}catch{}});
const statuses={queued:'排队中',fetching:'正在导入',review:'待你确认',published:'已分享',failed:'需要补充'};
let hasMoreShares=false;
let isLoadingMore=false;
const PAGE_SIZE=12;
function getHotspotCategory(s){
  const p=String(s.metadata?.platform||'').toLowerCase();
  const site=String(s.metadata?.site_name||'').toLowerCase();
  const u=String(s.url||'').toLowerCase();
  const title=String(s.title||'').toLowerCase();
  if(p.includes('telegram')||u.includes('t.me')||p.includes('youtube')||u.includes('youtu.be')||p.includes('twitter')||p==='x'||u.includes('x.com')||u.includes('twitter.com'))return 'social';
  if(site.includes('nasa')||u.includes('nasa.gov')||site.includes('science')||u.includes('sciencedaily')||title.includes('moon')||title.includes('crater')||title.includes('nasa'))return 'science';
  if(site.includes('the verge')||u.includes('theverge.com')||site.includes('ars technica')||u.includes('arstechnica.com')||site.includes('github')||u.includes('github')||title.includes('ai-generated')||title.includes('smart glasses')||title.includes('specs ai')||title.includes('tech'))return 'tech';
  return 'news';
}
function toast(message){const el=$('#toast');el.textContent=message;el.classList.add('show');clearTimeout(timer);timer=setTimeout(()=>el.classList.remove('show'),3500);}
async function api(path,data,method){const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),30000);try{let res;try{res=await fetch(path,{method:method||(data?'POST':'GET'),headers:data?{'Content-Type':'application/json'}:{},body:data?JSON.stringify(data):undefined,credentials:'same-origin',signal:controller.signal});}catch(e){throw new Error(e.name==='AbortError'?'请求超时；若刚才提交了内容，请刷新确认是否已保存。':'暂时连不上空间，请检查网络后重试。');}let result;try{result=await res.json();}catch{throw new Error('空间返回了异常响应，请检查服务地址或稍后再试。');}if(!res.ok){if(res.status===401&&user){user=null;closeModal();renderLogin();}throw new Error(result.error||'操作没有成功，请重试');}return result;}finally{clearTimeout(timeout);}}
async function action(button,fn){if(button.disabled)return;button.disabled=true;try{await fn();}catch(e){toast(e.message);}finally{button.disabled=false;}}
function date(ts){return new Date(ts*1000).toLocaleDateString('zh-CN',{month:'long',day:'numeric'});}
function source(s){try{return s.url?new URL(s.url).hostname:'手动分享';}catch{return '手动分享';}}
function renderLogin(){
 $('#app').innerHTML=`<main class="login"><section class="login-art">${brand}<h1>有些新鲜事，<br>只想<em>和你一起看。</em></h1><p>把远处的故事，带到彼此身边。<br>属于两个人的分享、回应和小小讨论。</p>${art}<div class="privacy-note">${icon('lock')} 仅对你们两个人开放</div></section><section class="login-form-wrap"><form class="login-form" id="login-form">${brand}<div class="eyebrow">OUR LITTLE CORNER</div><h2>欢迎回到小空间</h2><p>选好你的身份，输入专属口令。<br>有趣的事，等你一起看。</p><label for="login-user">你的身份</label><select id="login-user" name="user"><option value="one">账号一</option><option value="two">账号二</option></select><label for="login-password">登录口令</label><input id="login-password" name="password" type="password" autocomplete="current-password" placeholder="输入你的专属口令" required minlength="12"><div id="login-error" class="error-text" role="alert"></div><button class="primary" type="submit">进入我们的小空间 ${icon('arrow')}</button><div class="privacy-note">${icon('lock')} 口令由空间创建者提供</div></form></section></main>`;
 $('#login-form').onsubmit=e=>{e.preventDefault();const btn=$('button[type=submit]',e.target);action(btn,async()=>{try{const r=await api('/api/login',{user:$('#login-user').value,password:$('#login-password').value});user=r.user;await refresh();renderShell();if(pendingLink){const value=pendingLink;pendingLink='';await submitSharedLink(value);}}catch(err){$('#login-error').textContent=err.message;}});};
}
async function submitSharedLink(link){try{await api('/api/link-preview',{text:link,note:''});await refresh();renderShell();toast('链接已接收，正在后台解析');}catch(err){toast(err.message||'发送失败，请稍后重试');}}
async function refresh(loadMore=false){const seq=++refreshSeq;let offset=loadMore?shares.filter(s=>s.source_kind===currentColumn).length:0;let sharesData;try{sharesData=await api(`/api/shares?source_kind=${currentColumn}&limit=${PAGE_SIZE}&offset=${offset}&summary=1`);}catch{sharesData=await api('/api/shares?summary=1');}const result=await Promise.all([Promise.resolve(sharesData),api('/api/system').catch(()=>null)]);if(seq!==refreshSeq)return false;const newItems=result[0].shares||[];hasMoreShares=!!result[0].has_more;systemState=result[1];if(loadMore){const existingIds=new Set(shares.map(s=>s.id));for(const item of newItems){if(!existingIds.has(item.id))shares.push(item);}}else{const otherCol=shares.filter(s=>s.source_kind!==currentColumn);shares=[...otherCol,...newItems];}return true;}
function connectionText(){return !systemState?'暂时无法读取采集状态':systemState.worker_recent_contact?'采集服务最近已连接，可以接收新任务。':'最近一分钟未收到采集服务联系。已保存内容仍可阅读；新导入需要等待采集服务连接。';}
function phaseText(s){return s.status==='queued'?(s.error?'临时失败后已重新排队，将自动重试。':'等待采集服务领取任务。'):({reading:'正在读取原网页…',media:'正在下载并传输视频…',images:'正在保存正文图片…',finishing:'正在整理分享内容…'}[s.phase]||'正在整理正文和媒体…');}
function navButton(id,label,ico){
  const isActive=(id==='system'&&currentColumn==='system')||(id!=='system'&&currentColumn==='manual'&&filter===id);
  let n=0;
  if(id==='unread')n=shares.filter(s=>!isSystemShare(s)&&s.status==='published'&&s.author!==user.id&&!s.read).length;
  else if(id==='system')n=shares.filter(s=>isSystemShare(s)&&s.status==='published'&&!s.read).length;
  return `<button data-nav="${id}" class="${isActive?'active':''}" ${isActive?'aria-current="page"':''}>${icon(ico)}<span>${label}</span>${n?`<b>${n}</b>`:''}</button>`;
}
function avatarMarkup(id,alt='',className='avatar'){const src=id==='system'?'/avatar-system.svg':id==='two'?'/avatar-peach.svg':'/avatar-orange.svg';return `<img class="${className} avatar-art" src="${src}" alt="${esc(alt)}">`;}
function renderShell(){
 $('#app').innerHTML=`<aside class="rail">${brand}<nav class="nav" aria-label="主导航"><div class="nav-section-title">两个人的分享</div>${navButton('all','一起看看','home')}${navButton('unread','等你来看','mail')}${navButton('mine','我的分享','heart')}<div class="nav-section-title">全网发现</div>${navButton('system','系统热点','spark')}</nav><div class="rail-bottom"><div class="space-card"><div class="avatars">${avatarMarkup('one','账号一')}${avatarMarkup('two','账号二','avatar pink')}</div><strong>两个人，刚刚好。</strong><p>有来有往，分享日常里的小事。</p></div><nav class="nav">${navButton('settings','我们的小空间','user')}</nav><p class="rail-foot">A LITTLE CLOSER, EVERY DAY</p></div></aside><main class="main"><header class="topbar"><span class="desktop-date">${new Date().toLocaleDateString('zh-CN',{year:'numeric',month:'long',day:'numeric',weekday:'long'})}</span><span class="mobile-brand">一起看 <span class="muted">/</span></span><div class="row"><span class="online">双人私密空间</span><span class="user-name">${esc(user.name)}</span>${avatarMarkup(user.id,user.name)}</div></header><div id="main-content"></div></main><nav class="bottom-nav" aria-label="手机导航">${navButton('all','看分享','home')}${navButton('system','热点','spark')}<button class="new-share" data-new aria-label="新建分享">${icon('plus')}</button>${navButton('unread','等你看','mail')}${navButton('settings','我们','user')}</nav>`;
 document.querySelectorAll('[data-nav]').forEach(el=>el.onclick=()=>{
   const id=el.dataset.nav;
   if(id==='system'){currentColumn='system';filter='all';}
   else if(id==='settings'){filter='settings';}
   else{currentColumn='manual';filter=id;}
   renderShell();
   refresh().then(()=>renderCards());
 });
 $('[data-new]').onclick=()=>newShare();
 renderContent();
}
function renderContent(){
 if(filter==='settings'){
  $('#main-content').innerHTML=`<section class="hero"><div><div class="eyebrow">JUST YOU & ME</div><h1>我们的小空间</h1><p>所有分享，只留给彼此。</p></div></section><section class="settings-card"><h2>你好，${esc(user.name)}</h2><div class="privacy-note">${icon('lock')} 当前身份：${user.id==='one'?'账号一':'账号二'}</div><p>这里的正文和已导入图片来自同一个服务。原网页、视频播放器和动态评论区不会自动加载。</p><p>已发布的分享对双方可见，待确认的内容仅你可见。你可以删除自己发布的内容。</p><p>当前空间地址</p><div class="value">${esc(location.origin)}</div><p>当前版本固定连接以上空间，无需手动配置；登录口令请向空间创建者获取。</p><div class="connection-card"><strong>连接检查</strong><p id="connection-status">${esc(connectionText())}</p><button class="secondary" id="check-connection">重新检查</button><p class="small muted">版本 0.5 · 采集服务是否能读取某个网站，还需实际导入验证。</p></div><button class="secondary" id="logout">退出当前身份</button></section>`;
  $('#check-connection').onclick=e=>action(e.currentTarget,async()=>{systemState=await api('/api/system');$('#connection-status').textContent=connectionText();toast('连接状态已更新');});
  $('#logout').onclick=e=>action(e.currentTarget,async()=>{await api('/api/logout',{});user=null;shares=[];renderLogin();});return;
 }
 const isSystem=currentColumn==='system';
 const title=isSystem?'世界很大，<br><em>挑些热点一起看。</em>':(filter==='unread'?'你还没看的，<br><em>都替你留着。</em>':filter==='mine'?'想分享的事，<br><em>都在这里。</em>':'看到有趣的，<br><em>第一个想告诉你。</em>');
 const desc=isSystem?'由系统自动汇聚的外部平台热点，原作者与平台出处清晰保留。':'分享一个瓜，也分享一句「你怎么看」。';
 const colTabs=`<div class="column-tabs" role="tablist" aria-label="顶层栏目切换"><button class="column-tab ${!isSystem?'active':''}" data-switch-col="manual" role="tab" aria-selected="${!isSystem}">${icon('heart')} 我们的分享</button><button class="column-tab ${isSystem?'active':''}" data-switch-col="system" role="tab" aria-selected="${isSystem}">${icon('spark')} 系统热点</button></div>`;
 const bannerOrPrompt=isSystem?`<div class="hotspot-banner"><span class="prompt-icon">${icon('spark')}</span><span class="text"><strong>系统热点自动同步中</strong><p>来自公开频道的精选候选，讨论和回应只在你们之间可见</p></span><span class="badge system-badge">系统推送</span></div>`:`<button class="share-prompt" id="new-share"><span class="prompt-icon">${icon('link')}</span><span class="text"><strong>今天有什么想和 TA 分享？</strong><p>贴个链接，或写下你发现的新鲜事</p></span><span class="plus-circle">${icon('plus')}</span></button>`;
 const filterPills=isSystem?`<div class="filters"><button data-filter="all" class="${filter==='all'?'active':''}">全部热点</button><button data-filter="tech" class="${filter==='tech'?'active':''}">科技数码</button><button data-filter="science" class="${filter==='science'?'active':''}">探索科学</button><button data-filter="news" class="${filter==='news'?'active':''}">环球时事</button><button data-filter="social" class="${filter==='social'?'active':''}">社交动态</button></div>`:`<div class="filters"><button data-filter="all" class="${filter==='all'?'active':''}">全部分享</button><button data-filter="unread" class="${filter==='unread'?'active':''}">等你来看</button><button data-filter="mine" class="${filter==='mine'?'active':''}">我的分享</button></div>`;
  $('#main-content').innerHTML=`<section class="hero"><div><div class="eyebrow">${isSystem?'SYSTEM HOTSPOTS':'LITTLE STORIES, SHARED WITH YOU'}</div><h1>${title}</h1><p>${desc}</p></div>${art}</section>${colTabs}${bannerOrPrompt}<div class="section-line">${filterPills}<span class="small muted" id="count"></span></div><div class="cards" id="cards"></div><p class="end-note">小小的分享，让我们靠近一点 ♡</p>`;
  document.querySelectorAll('[data-switch-col]').forEach(el=>el.onclick=()=>{currentColumn=el.dataset.switchCol;filter='all';renderShell();refresh().then(()=>renderCards());});
  if($('#new-share'))$('#new-share').onclick=()=>newShare();
  document.querySelectorAll('[data-filter]').forEach(el=>el.onclick=()=>{filter=el.dataset.filter;renderShell();});
  renderCards();
}
function renderCards(){
  const unread=shares.filter(s=>!isSystemShare(s)&&s.status==='published'&&s.author!==user.id&&!s.read).length;
  const sysUnread=shares.filter(s=>isSystemShare(s)&&s.status==='published'&&!s.read).length;
  document.querySelectorAll('[data-nav="unread"]').forEach(el=>{let badge=el.querySelector('b');if(!unread){if(badge)badge.remove();}else{if(!badge){badge=document.createElement('b');el.appendChild(badge);}badge.textContent=String(unread);}});
  document.querySelectorAll('[data-nav="system"]').forEach(el=>{let badge=el.querySelector('b');if(!sysUnread){if(badge)badge.remove();}else{if(!badge){badge=document.createElement('b');el.appendChild(badge);}badge.textContent=String(sysUnread);}});
  if(!$('#cards'))return;
  const isSystem=currentColumn==='system';
  let list;
  if(isSystem){
   list=shares.filter(s=>isSystemShare(s)&&s.status==='published');
   if(filter!=='all'){
     list=list.filter(s=>getHotspotCategory(s)===filter);
   }
  }else{
   list=shares.filter(s=>!isSystemShare(s)&&(filter==='mine'?s.author===user.id:filter==='unread'?s.status==='published'&&s.author!==user.id&&!s.read:s.author===user.id||['queued','fetching','published'].includes(s.status)));
  }
  $('#count').textContent=`${list.length} 条${isSystem?'热点':'小分享'}`;
  const cardsHtml=list.map((s,i)=>{
    const image=s.blocks.find(b=>b.type==='image');const video=s.blocks.find(b=>b.type==='video');const pending=s.status!=='published';
    const isSys=isSystemShare(s);
    const unreadBadge=!pending&&!s.read&&(isSys||s.author!==user.id);
    const cover=image?.src||(image?.asset?'/media/'+image.asset:'')||video?.poster||(video?.poster_asset?'/media/'+video.poster_asset:'');
    const authorDisplay=isSys?'系统推送':s.author_name;
    const authorRole=isSys?'':'分享';
    const authorId=isSys?'system':s.author;
    const platformName=s.metadata?.platform||(isSys?'热点':source(s));
    const noteClean=cleanNote(s.note);
    return `<article class="card"><button class="card-open" data-open="${s.id}"><div class="cover ${cover?'':'has-art'}">${cover?`<img src="${esc(cover)}" alt="${esc(s.title)}" loading="lazy">`:generateArtCover(s,i)}${video?'<span class="play-badge">▶</span>':''}${isSys?`<span class="badge system-badge">${esc(platformName)}</span>`:pending?`<span class="badge warn">${statuses[s.status]}</span>`:unreadBadge?'<span class="badge">等你来看</span>':''}</div><div class="card-content"><div class="source"><span class="source-dot"></span>${esc(s.metadata?.site_name||platformName)}${s.metadata?.author?` · ${esc(s.metadata.author)}`:''}<span>·</span>${date(s.published||s.created)}</div><h2>${esc(s.title)}</h2>${noteClean?`<p class="personal-note">${esc(noteClean.slice(0,100))}</p>`:''}</div></button><div class="card-footer"><div class="row">${avatarMarkup(authorId,authorDisplay,'mini-avatar')}<span>${esc(authorDisplay)}${authorRole?' '+authorRole:''}</span></div><span>${s.reactions.map(r=>r.emoji).join(' ')} ${s.comments.length?`${s.comments.length} 条回应`:'等一句你的看法'}</span></div></article>`;
  }).join('');
  const loadMoreBtn=(hasMoreShares&&filter==='all')?`<div class="load-more-wrap"><button class="secondary load-more-btn" id="load-more-btn">${isLoadingMore?'正在加载…':'加载更多 '+(isSystem?'热点':'分享')+' ▾'}</button></div>`:'';
  $('#cards').innerHTML=list.length?(cardsHtml+loadMoreBtn):(isSystem
    ? `<div class="empty"><div class="empty-icon">⚡</div><h2>暂无${filter==='all'?'系统热点':'该分类热点'}</h2><p>当前分类下暂无已同步的内容。<br>新内容采集后将自动出现在这里。</p><button class="secondary" id="empty-refresh">刷新检查 ${icon('arrow')}</button></div>`
    : `<div class="empty"><div class="empty-icon">${filter==='unread'?'✉':'🍉'}</div><h2>${filter==='unread'?'已经看过所有分享啦':'第一条分享，等你来写'}</h2><p>${filter==='unread'?'新的有趣故事到来时，会出现在这里。':'贴一个公开文章链接，或写下今天的新发现。<br>发布前可以先预览，准备好再分享给 TA。'}</p><button class="primary" id="empty-new">分享一点新鲜事 ${icon('plus')}</button></div>`
  );
  if($('#load-more-btn')){
    $('#load-more-btn').onclick=async(e)=>{
      if(isLoadingMore)return;
      isLoadingMore=true;
      e.currentTarget.textContent='正在加载…';
      e.currentTarget.disabled=true;
      try{
        await refresh(true);
        renderCards();
      }catch(err){
        toast('加载失败，请重试');
      }finally{
        isLoadingMore=false;
      }
    };
  }
  document.querySelectorAll('[data-open]').forEach(el=>el.onclick=()=>openShare(el.dataset.open));
  if($('#empty-new'))$('#empty-new').onclick=()=>newShare();
  if($('#empty-refresh'))$('#empty-refresh').onclick=async()=>{await refresh();renderCards();toast('已刷新');};
}
let modalHistoryPushed=false;
function modal(title,body,wide=false){
  trSeq++;lastFocus=document.activeElement;
  $('#modal-root').innerHTML=`<div class="modal-backdrop"><section class="modal ${wide?'viewer':''}" role="dialog" aria-modal="true" aria-label="${esc(title)}"><header class="modal-head"><h2>${esc(title)}</h2><button class="close" id="close-modal" aria-label="关闭">×</button></header><div class="modal-body">${body}</div></section></div>`;
  document.body.style.overflow='hidden';
  try{if(window.AndroidBridge&&window.AndroidBridge.setModalOpen)window.AndroidBridge.setModalOpen(true);}catch(e){}
  if(!modalHistoryPushed){
    try{history.pushState({modal:true},'');modalHistoryPushed=true;}catch(e){}
  }
  $('#close-modal').onclick=()=>closeModal();
  $('#modal-root .modal-backdrop').onclick=e=>{if(e.target===e.currentTarget)closeModal();};
  $('#close-modal').focus();
}
function closeModal(popHistory=true){
  currentId=null;$('#modal-root').innerHTML='';document.body.style.overflow='';
  try{if(window.AndroidBridge&&window.AndroidBridge.setModalOpen)window.AndroidBridge.setModalOpen(false);}catch(e){}
  if(lastFocus?.isConnected)lastFocus.focus();
  if(modalHistoryPushed){
    modalHistoryPushed=false;
    if(popHistory&&history.state?.modal){try{history.back();}catch(e){}}
  }
}
window.addEventListener('popstate',e=>{if($('#modal-root .modal'))closeModal(false);});
window.closeModal=closeModal;
window.handleAndroidBack=function(){const m=$('#modal-root .modal');if(m){closeModal(true);return true;}return false;};
document.addEventListener('keydown',e=>{const m=$('#modal-root .modal');if(!m)return;if(e.key==='Escape')closeModal();if(e.key==='Tab'){const els=[...m.querySelectorAll('button,a,input,textarea,select')].filter(x=>!x.disabled&&x.getClientRects().length);const first=els[0],last=els.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}}});
function newShare(link='',manual=false){currentId=null;modal('分享一点新鲜事',`<div class="tabs"><button id="link-tab" class="${manual?'':'active'}">链接导入</button><button id="manual-tab" class="${manual?'active':''}">自己写一条</button></div><form id="share-form">${manual?'<label for="share-title">给它起个标题</label><input id="share-title" required maxlength="300" placeholder="今天看到的一件有趣的事"><label for="share-body">分享内容</label><textarea id="share-body" required maxlength="50000" rows="6" placeholder="写下来，也可以在下一步补充截图…"></textarea>':''}<label for="share-url">${manual?'原始出处（选填）':'粘贴分享内容或链接'}</label>${manual?`<input id="share-url" type="url" maxlength="2000" placeholder="https://…" value="${esc(link)}">`:`<textarea id="share-url" required maxlength="10000" rows="4" placeholder="可以粘贴抖音、TikTok、X、Telegram 的完整分享文案，也可以只粘贴链接">${esc(link)}</textarea>`}<label for="share-note">想对 TA 说的话</label><textarea id="share-note" maxlength="1000" rows="2" placeholder="这个反转有点意思，你怎么看？"></textarea><p class="helper">${manual?'内容保存后先进入预览，确认好了再发布。':'粘贴后系统会自动识别其中的链接，在后台解析标题、封面、正文和视频；无需先整理成纯网址。'}</p><div class="error-text" id="share-error" role="alert"></div><div class="form-actions"><button class="primary" type="submit">${manual?'保存并预览':'发送并解析'} ${icon('arrow')}</button></div></form>`);
 $('#link-tab').onclick=()=>newShare($('#share-url').value,false);$('#manual-tab').onclick=()=>newShare($('#share-url').value,true);
 $('#share-form').onsubmit=e=>{e.preventDefault();action($('button[type=submit]',e.target),async()=>{try{let r;if(manual){r=await api('/api/shares',{manual:true,url:$('#share-url').value,note:$('#share-note').value,title:$('#share-title').value,body:$('#share-body').value});}else{r=await api('/api/link-preview',{text:$('#share-url').value,note:$('#share-note').value});}await refresh();renderCards();if(manual)await openShare(r.id);else{closeModal();renderShell();toast('已发送，系统正在解析内容');}}catch(err){$('#share-error').textContent=err.message;}});};
}
function renderBlocks(blocks,editable=false){
 return blocks.map(b=>{
  if(b.type==='image')return `<figure><img src="${esc(b.src)}" alt="${esc(b.alt)}" loading="lazy">${b.alt?`<figcaption>${esc(b.alt)}</figcaption>`:''}${editable?`<button class="quiet danger remove-image" data-remove-image="${esc(b.asset)}">移除这张图片</button>`:''}</figure>`;
  if(b.type==='video')return `<figure class="video-wrap"><video controls preload="metadata" playsinline ${b.poster?`poster="${esc(b.poster)}"`:''}><source src="${esc(b.src)}" type="video/mp4">当前设备无法播放这个视频。</video></figure>`;
  if(b.type==='heading')return `<h3>${esc(b.text)}</h3>`;
  if(b.type==='quote')return `<blockquote>${esc(b.text)}</blockquote>`;
  return `<p>${esc(b.text)}</p>`;
 }).join('');
}
async function openShare(id){
 try{
  const s=(await api('/api/shares/'+id)).share;
  const isSys=isSystemShare(s);
  const own=!isSys&&s.author===user.id;
  const published=s.status==='published';
  const waiting=['queued','fetching'].includes(s.status);
  const modalTitle=published?(isSys?'和你一起看 · 系统热点':'和你一起看'):'发布前，先看一眼';
   const sourceText=isSys?`${esc(s.metadata?.platform||'系统热点')} · 系统推送`:esc(source(s));
   const authorDisplay=isSys?'系统推送':s.author_name;
   const noteClean=cleanNote(s.note);
   const displayWarnings=(s.warnings||[]).filter(w=>!w.includes('测试版仅')&&!w.includes('未导入播放器')&&!w.includes('图片未导入')&&!w.includes('结构化文章数据')&&!w.includes('已导入原页视频'));

   modal(modalTitle,`${!published?`<div class="notice ${s.status==='review'?'good':''}" id="import-status"><strong>${statuses[s.status]}</strong><br>${waiting?esc(phaseText(s))+'<br>'+esc(connectionText()):s.status==='failed'?esc(s.error):'检查内容是否完整，确认后 TA 才能看到。'}</div>`:''}${displayWarnings.map(w=>`<div class="notice">${esc(w)}</div>`).join('')}<div class="source">${sourceText}</div><div class="translate-row"><button class="translate-btn" id="translate-btn">翻译</button><span class="translate-hint">正文与标题可译为中文</span></div><h1 class="article-title">${esc(s.title)}</h1><div class="article-meta"><span class="author-tag">${isSys?'【系统推送】':esc(authorDisplay)}</span> · ${date(s.created)}${s.fetched?` · ${date(s.fetched)} 保存的内容快照`:''}</div>${s.metadata?.platform?`<p class="article-meta">来源平台：<strong>${esc(s.metadata.platform)}</strong></p>`:''}${s.metadata?.author?`<p class="article-meta">原作者：${esc(s.metadata.author)}</p>`:''}${s.metadata?.published_at?`<p class="article-meta">原页标注时间：${esc(s.metadata.published_at)}</p>`:''}${noteClean?`<p class="personal-note">${esc(noteClean)}</p>`:''}${own&&waiting?'<div class="article-actions"><button class="secondary" id="cancel-import">停止导入，改为手动补充</button></div>':''}<div class="article-body">${renderBlocks(s.blocks,own&&!published&&!waiting)}</div>${s.url?`<p class="article-meta">原始出处：<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(source(s))} ↗</a><br>打开原网页需要能够访问该网站；这里展示的是已保存内容。</p>`:''}${own&&!published&&!waiting?`<div class="article-actions">${s.status==='review'?'<button class="primary" id="publish">确认发布给 TA</button>':''}<button class="secondary" id="edit">补充正文</button>${s.status==='failed'?'<button class="secondary" id="retry">重新导入</button>':''}</div><label for="image-upload">补充图片（选填，单张最大 5 MB）</label><input type="file" accept="image/jpeg,image/png,image/webp,image/gif" id="image-upload" class="file-input">`:''}${published?`<div class="article-actions">${['👀','❤️','🤔','😂'].map(emoji=>`<button class="reaction ${s.reactions.some(r=>r.user_id===user.id&&r.emoji===emoji)?'selected':''}" data-react="${emoji}" aria-label="回应 ${emoji}" aria-pressed="${s.reactions.some(r=>r.user_id===user.id&&r.emoji===emoji)}">${emoji} <small>${s.reactions.filter(r=>r.emoji===emoji).length||''}</small></button>`).join('')}</div><section class="discussion"><h3 id="discussion-title">听听你的看法 <span class="muted">· ${s.comments.length}</span></h3><div id="comments">${s.comments.map(c=>`<div class="comment"><span class="avatar">${esc(c.name.slice(-1))}</span><div><div class="name">${esc(c.name)} · ${date(c.created)}</div><p class="bubble">${esc(c.body)}</p></div></div>`).join('')||'<p class="small muted">说说你的第一反应，TA 想听。</p>'}</div><form class="comment-form" id="comment-form"><input id="comment-body" placeholder="我觉得…" maxlength="2000" required aria-label="评论内容"><button class="primary" type="submit">发送</button></form></section>`:''}${isSys?'<div class="form-actions"><button class="quiet danger" id="delete-share">从热点中移除</button></div>':(own?'<div class="form-actions"><button class="quiet danger" id="delete-share">删除这条分享</button></div>':'')}`,true);
  currentId=id;
  bindTranslate();
  if(published){await api('/api/shares/'+id+'/read',{}).catch(()=>{});const cached=shares.find(x=>x.id===id);if(cached)cached.read=true;renderCards();}
  if($('#cancel-import'))$('#cancel-import').onclick=e=>action(e.currentTarget,async()=>{await api('/api/shares/'+id+'/cancel',{});await refresh();renderCards();const stopped=(await api('/api/shares/'+id)).share;editShare(stopped);});
  document.querySelectorAll('[data-remove-image]').forEach(el=>el.onclick=()=>action(el,async()=>{await api('/api/shares/'+id+'/remove-image',{asset:el.dataset.removeImage});await refresh();renderCards();await openShare(id);toast('图片已移除');}));
  if($('#publish'))$('#publish').onclick=e=>action(e.currentTarget,async()=>{await api('/api/shares/'+id+'/publish',{});toast('已分享，等 TA 一起来看');await refresh();renderCards();await openShare(id);});
  if($('#retry'))$('#retry').onclick=e=>action(e.currentTarget,async()=>{await api('/api/shares/'+id+'/retry',{});await refresh();renderCards();await openShare(id);});
  if($('#edit'))$('#edit').onclick=()=>editShare(s);
  if($('#image-upload'))$('#image-upload').onchange=async e=>{const f=e.target.files[0];if(!f)return;if(f.size>5*1024*1024){toast('请选择小于 5 MB 的图片');return;}e.target.disabled=true;try{const value=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=reject;r.readAsDataURL(f);});await api('/api/shares/'+id+'/image',{data:value});await refresh();renderCards();await openShare(id);toast('图片已补充');}catch(err){toast(err.message||'图片读取失败');e.target.disabled=false;}};
  document.querySelectorAll('[data-react]').forEach(el=>el.onclick=()=>action(el,async()=>{const emoji=el.classList.contains('selected')?'':el.dataset.react;await api('/api/shares/'+id+'/reaction',{emoji});await refresh();renderCards();await openShare(id);}));
  if($('#comment-form'))$('#comment-form').onsubmit=e=>{e.preventDefault();action($('button',e.target),async()=>{await api('/api/shares/'+id+'/comment',{body:$('#comment-body').value});await refresh();renderCards();await openShare(id);$('.modal').scrollTop=$('.modal').scrollHeight;});};
  if($('#delete-share'))$('#delete-share').onclick=e=>action(e.currentTarget,async()=>{
    const promptMsg=isSys?'移除后双方将不再看到此热点，且系统不会重复推送。确定从热点中移除？':'删除后，双方都无法再看到这条分享及其评论。确定删除？';
    if(!confirm(promptMsg))return;
    await api('/api/shares/'+id,{},'DELETE');closeModal();await refresh();renderShell();toast(isSys?'热点已移除':'分享已删除');
  });
 }catch(err){toast(err.message);}
}
function editShare(s){currentId=null;modal('补充内容',`<form id="edit-form"><label for="edit-title">标题</label><input id="edit-title" value="${esc(s.title)}" maxlength="300" required><label for="edit-body">正文（已有图片会保留）</label><textarea id="edit-body" rows="10" maxlength="50000" required>${esc(s.blocks.filter(b=>b.type!=='image').map(b=>b.text).join('\n\n'))}</textarea><label for="edit-url">原始出处（选填）</label><input id="edit-url" type="url" maxlength="2000" value="${esc(s.url)}" placeholder="https://…"><p class="helper">更换或清空出处会清除原采集时间，正文不会自动重新抓取。</p><label for="edit-note">想对 TA 说的话</label><textarea id="edit-note" maxlength="1000">${esc(s.note)}</textarea><div class="form-actions"><button class="primary" type="submit">保存并预览</button></div></form>`);$('#edit-form').onsubmit=e=>{e.preventDefault();action($('button',e.target),async()=>{await api('/api/shares/'+s.id+'/edit',{title:$('#edit-title').value,body:$('#edit-body').value,note:$('#edit-note').value,url:$('#edit-url').value});await refresh();renderCards();await openShare(s.id);});};}
function syncComments(s){const el=$('#comments');if(!el)return;const pane=$('.modal');const scroll=pane.scrollTop;el.innerHTML=s.comments.map(c=>`<div class="comment"><span class="avatar">${esc(c.name.slice(-1))}</span><div><div class="name">${esc(c.name)} · ${date(c.created)}</div><p class="bubble">${esc(c.body)}</p></div></div>`).join('')||'<p class="small muted">说说你的第一反应，TA 想听。</p>';$('#discussion-title').textContent='听听你的看法 · '+s.comments.length;pane.scrollTop=scroll;}
// ---------------- server-side translation (Gemini / Server relay) -------------
let trSeq=0;
function bindTranslate(){
  const btn=$('#translate-btn');
  if(!btn)return;
  btn.onclick=e=>action(e.currentTarget,async()=>{
    const title=$('.article-title'),body=$('.article-body');
    const nodes=body?[...body.querySelectorAll('p,h3,blockquote')]:[];
    if(btn.dataset.on){
      if(title&&title.dataset.orig)title.textContent=title.dataset.orig;
      nodes.forEach(el=>{if(el.dataset.orig)el.innerHTML=el.dataset.orig;});
      if(body)body.querySelectorAll('.tr-extra, .tr-added-body').forEach(el=>el.remove());
      btn.textContent='翻译';delete btn.dataset.on;return;
    }
    if(title)title.dataset.orig=title.textContent;
    nodes.forEach(el=>el.dataset.orig=el.innerHTML);
    btn.textContent='翻译中…';btn.disabled=true;
    const seq=++trSeq;
    try{
      const titleText=title?title.textContent:'';
      const bodyText=nodes.map(el=>el.textContent.trim()).filter(Boolean).join('\n\n');
      const r=await api('/api/translate',{title:titleText,body:bodyText});
      if(seq!==trSeq)return;
      if(title&&r.title)title.textContent=r.title;
      if(body&&r.body){
        const paragraphs=r.body.split('\n\n').map(p=>p.trim()).filter(Boolean);
        if(nodes.length>0){
          nodes.forEach((el,i)=>{
            if(i<paragraphs.length){
              el.textContent=paragraphs[i];
            }
          });
          if(paragraphs.length>nodes.length){
            const extraHtml=paragraphs.slice(nodes.length).map(p=>'<p class="tr-extra">'+esc(p)+'</p>').join('');
            nodes[nodes.length-1].insertAdjacentHTML('afterend',extraHtml);
          }
        }else if(paragraphs.length>0){
          const pContainer=document.createElement('div');
          pContainer.className='tr-added-body';
          pContainer.innerHTML=paragraphs.map(p=>'<p class="tr-extra">'+esc(p)+'</p>').join('');
          body.appendChild(pContainer);
        }
      }
      btn.textContent='原文';btn.dataset.on='1';
    }catch(err){
      if(seq!==trSeq)return;
      if(title&&title.dataset.orig)title.textContent=title.dataset.orig;
      nodes.forEach(el=>{if(el.dataset.orig)el.innerHTML=el.dataset.orig;});
      if(body)body.querySelectorAll('.tr-extra, .tr-added-body').forEach(el=>el.remove());
      toast(err.message||'翻译服务暂时不可用，请稍后再试');
    }finally{btn.disabled=false;}
  });
}
// ---------------- pull-to-refresh (single client implementation) -------------
// Only acts when the list is scrolled to the top, no modal / focused input /
// fullscreen video / horizontal gesture. Never clears login, never rebuilds
// the WebView, and leaves any in-progress share/comment text untouched.
const PULL_DISTANCE = 70, PULL_MAX = 120;
const pullEl=document.createElement('div');
pullEl.id='pull-indicator';pullEl.setAttribute('aria-live','polite');
pullEl.textContent='下拉刷新';
document.body.appendChild(pullEl);
function pullCanStart(){
  if(pull.refreshing||pollBusy)return false;
  if(document.querySelector('#modal-root .modal'))return false;      // detail/edit/comment modal open
  const el=document.activeElement;
  if(el&&/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))return false;   // login/editor input focused
  if(document.fullscreenElement)return false;                          // fullscreen video
  if(window.scrollY>0)return false;                                    // not at list top
  return filter!=='settings' && !!$('#cards');                         // list page only
}
function pullSet(state,dy){
  pullEl.classList.toggle('show',state);
  pullEl.style.transform=dy?'translateY('+Math.min(dy*.45,PULL_MAX)+'px)':'';
}
function pullAbort(){ // cancel an in-progress gesture: system interrupt or horizontal swipe
  pull.pulling=false;pull.dy=0;pull.cancelled=false;pullSet(false,0);
}
window.addEventListener('touchstart',e=>{
  if(!pullCanStart())return;
  const t=e.touches[0];pull.y=t.clientY;pull.sx=t.clientX;pull.dy=0;pull.cancelled=false;pull.pulling=true;
},{passive:true});
window.addEventListener('touchmove',e=>{
  if(!pull.pulling||pull.cancelled)return;
  const t=e.touches[0];
  const dy=t.clientY-pull.y;
  const dx=t.clientX-pull.sx;
  if(Math.abs(dx)>Math.abs(dy)&&Math.abs(dx)>8){pullAbort();return;} // horizontal swipe cancels any vertical residue
  pull.dy=Math.max(0,dy);
  if(pull.dy>2){pullEl.textContent=pull.dy>=PULL_DISTANCE?'松开刷新':'下拉刷新';pullSet(true,pull.dy);}
  else pullSet(false,0);
},{passive:true});
window.addEventListener('touchcancel',e=>{if(pull.pulling)pullAbort();},{passive:true});
window.addEventListener('touchend',async()=>{
  if(!pull.pulling)return;
  const cancelled=pull.cancelled;
  const dy=pull.dy;
  pull.pulling=false;pull.dy=0;pull.cancelled=false;
  if(cancelled){pullSet(false,0);return;}
  if(dy<PULL_DISTANCE){pullSet(false,0);return;}
  pull.refreshing=true;pullEl.textContent='正在刷新…';pullSet(true,PULL_DISTANCE);
  try{
    const ok=await refresh();
    if(ok){renderCards();pullEl.textContent='已更新';toast('已更新');}
    else pullEl.textContent='已更新';
  }catch(err){
    pullEl.textContent='刷新失败，可再试一次';toast(err.message||'刷新失败，请稍后重试');
    // list keeps the previous content
  }
  setTimeout(()=>{pull.refreshing=false;pullSet(false,0);},500);
},{passive:true});

async function boot(){try{user=(await api('/api/me')).user;await refresh();renderShell();if(pendingLink){const value=pendingLink;pendingLink='';await submitSharedLink(value);}}catch{renderLogin();}if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>{});}
let lastPollTime=Date.now();
setInterval(async()=>{
  if(!user||document.hidden||pollBusy||pull.refreshing)return;
  const hasPending=shares.some(s=>['queued','fetching'].includes(s.status));
  const now=Date.now();
  if(!hasPending&&now-lastPollTime<25000)return;
  lastPollTime=now;
  pollBusy=true;
  try{
    const old=shares;
    await refresh();
    if(!user)return;
    if(currentId){
      const before=old.find(s=>s.id===currentId),after=shares.find(s=>s.id===currentId);
      if(before&&after&&(before.status!==after.status||before.phase!==after.phase))await openShare(currentId);
      else if(after?.status==='published'&&JSON.stringify(before?.comments)!==JSON.stringify(after.comments))syncComments(after);
    }
    renderCards();
  }catch{}finally{pollBusy=false;}
},5000);
boot();
