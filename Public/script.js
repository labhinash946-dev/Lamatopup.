async function readJson(r){const t=await r.text();try{return JSON.parse(t)}catch(e){return null}}
const data={freefire:{title:"Free Fire diamonds",subtitle:"Fast and secure purchase",icon:"assets/free-fire-logo.png",packages:[["25 Diamonds",35],["50 Diamonds",50],["115 Diamonds",100],["240 Diamonds",220],["610 Diamonds",550],["1240 Diamonds",1100],["2530 Diamonds",2130],["Weekly Membership",220,"assets/weekly-membership.jpg"],["Monthly Membership",1048,"assets/monthly-membership.jpg"]]},efootball:{title:"eFootball coins & packs",subtitle:"Coins and special packs in NPR",icon:"assets/efootball-logo.png",packages:[["130 Coins",210],["300 Coins",450],["750 Coins",1100],["1040 Coins",1420],["2130 Coins",2900],["3250 Coins",4350],["5700 Coins",7100],["12800 Coins",15150],["Starter Set: Casillas",420,"assets/casillas-pack.png"],["Luis Suarez Pack",200,"assets/suarez-pack.png"]]}};
Object.assign(data.freefire,{heroTitle:'Free Fire Diamond Top Up Nepal',about:'Garena Free Fire is a battle royale game where players fight to be the last one standing. Top up diamonds for exclusive characters, skins, and more.',bannerTitle:'Top Up Free Fire Without Login',bannerText:'Just enter your User ID / Player ID, pay with Fonepay QR, and we deliver your diamonds once payment is confirmed.',step1:'Enter Your Game ID',step2:'Select Diamonds Package',packIcon:'\u{1F48E}'});
Object.assign(data.efootball,{heroTitle:'eFootball Coins & Packs Top Up Nepal',about:'eFootball is a free-to-play football game. Top up coins and special packs to build your dream squad with the players you want.',bannerTitle:'Top Up eFootball Coins & Packs',bannerText:'Enter your KONAMI User ID and WhatsApp number. We never ask for your password.',step1:'Enter Your Account Details',step2:'Select Coins Package',packIcon:'\u{1FA99}'});
let pkgFilter='';
let selectedGame='freefire',selectedIndex=0,selectedPayment='esewa';
function chooseGame(game){selectedGame=game;selectedIndex=0;pkgFilter='';document.querySelector('#pkgSearch').value='';render();setTimeout(()=>document.querySelector('#orders').scrollIntoView({behavior:'smooth',block:'start'}),50);}
function render(){
const d=data[selectedGame],$=s=>document.querySelector(s);
$('#heroFreefireBtn').className=selectedGame==='freefire'?'primary':'secondary';
$('#heroEfootballBtn').className=selectedGame==='efootball'?'primary':'secondary';
$('#selectedIcon').innerHTML=`<img src="${d.icon}" alt="" class="mini-logo">`;
$('#ghBg').style.backgroundImage=`url(${d.icon})`;
$('#efootballDetails').classList.toggle('hidden',selectedGame!=='efootball');
$('#freefireDetails').classList.toggle('hidden',selectedGame==='efootball');
$('#selectedTitle').textContent=d.heroTitle;
$('#aboutText').textContent=d.about;
$('#bannerTitle').textContent=d.bannerTitle;
$('#bannerText').textContent=d.bannerText;
$('#step1Title').textContent=d.step1;
$('#step2Title').textContent=d.step2;
$('#sumGame').textContent=selectedGame==='freefire'?'Free Fire':'eFootball';
const box=$('#packages'),q=pkgFilter.trim().toLowerCase();let shown=0;box.innerHTML='';
d.packages.forEach((p,i)=>{
const match=!q||p[0].toLowerCase().includes(q)||String(p[1]).includes(q);
const b=document.createElement('button');b.className='package'+(i===selectedIndex?' active':'')+(match?'':' hidden');if(match)shown++;
b.innerHTML=`${p[2]?`<img src="${p[2]}" class="package-icon" alt="">`:`<span class="pk-ico">${d.packIcon}</span>`}${p[0]}<b>Rs. ${p[1]}</b>`;
b.onclick=()=>{selectedIndex=i;render()};box.appendChild(b)});
$('#pkgEmpty').classList.toggle('hidden',shown>0);
$('#sumPackage').textContent=d.packages[selectedIndex][0];
$('#sumTotal').textContent='Rs. '+d.packages[selectedIndex][1];
}
document.querySelectorAll('.payment').forEach(b=>b.onclick=()=>{selectedPayment=b.dataset.payment;document.querySelectorAll('.payment').forEach(x=>x.classList.remove('active'));b.classList.add('active')});
function showPayment(){if(selectedGame==='freefire' && !document.querySelector('#playerId').value.trim()){alert('Please enter your Free Fire Player ID / UID.');return}if(selectedGame==='efootball' && (!document.querySelector('#konamiId').value.trim() || !document.querySelector('#whatsapp').value.trim())){alert('Please enter your KONAMI User ID and WhatsApp number.');return}const d=data[selectedGame],p=d.packages[selectedIndex];document.querySelector('#modalText').textContent=`${d.title} • ${p[0]}`;const mn=document.querySelector('#modalName');mn.textContent=(selectedGame==='freefire'&&verifiedName)?'Player: '+verifiedName:'';document.querySelector('#modalUid').textContent=selectedGame==='freefire'?document.querySelector('#playerId').value.trim():document.querySelector('#konamiId').value.trim();document.querySelector('#modalAmount').textContent='Rs. '+p[1];document.querySelector('#modalMethod').textContent=selectedPayment==='esewa'?'eSewa / Khalti / Bank':'Wallet payment';document.querySelector('#paymentModal').classList.remove('hidden')}
function closePayment(){document.querySelector('#paymentModal').classList.add('hidden')}
let orderKey=(crypto.randomUUID?crypto.randomUUID():String(Date.now())+Math.random().toString(36).slice(2)).replace(/[^A-Za-z0-9_-]/g,'');
async function submitOrder(){
  const d=data[selectedGame], p=d.packages[selectedIndex];
  const userId=selectedGame==='freefire'?document.querySelector('#playerId').value.trim():document.querySelector('#konamiId').value.trim();
  try{
    const response=await fetch('/api/orders',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':orderKey},body:JSON.stringify({game:selectedGame,product_id:(selectedGame==='freefire'?'ff-':'ef-')+selectedIndex,user_id:userId,payment_ref:document.querySelector('#remarks').value.trim(),contact:selectedGame==='efootball'?document.querySelector('#whatsapp').value.trim():''})});
    const result=await readJson(response);
    if(!result) throw new Error('Server error. Please try again in a moment.');
    if(!response.ok||result.success===false) throw new Error(result.error||'Order failed');
    try{const saved=JSON.parse(localStorage.getItem('lamaOrders')||'[]');if(result.tracking_token)saved.unshift({id:result.order.id,token:result.tracking_token});localStorage.setItem('lamaOrders',JSON.stringify(saved.slice(0,20)))}catch(e){}
    orderKey=crypto.randomUUID?crypto.randomUUID().replace(/-/g,''):String(Date.now())+Math.random().toString(36).slice(2);
    alert('Order '+result.order.id+' submitted. We will process it once your payment is verified. Save this order ID.');
    closePayment();
  }catch(error){alert(error.message)}
}
function scrollToSection(id){document.getElementById(id).scrollIntoView({behavior:'smooth'})}
render();
let verifiedName='';
let nameTimer,nameBackend=true;
function playerBox(){return document.querySelector('#playerCard')}
function setStatus(text,cls){const b=playerBox();b.className='player-status '+(cls||'');b.textContent=text}
function clearPlayerName(){
  verifiedName='';clearTimeout(nameTimer);setStatus('','');
  const uid=document.querySelector('#playerId').value.trim();
  if(nameBackend&&/^\d{8,15}$/.test(uid)){setStatus('Waiting for you to finish typing...','wait');nameTimer=setTimeout(verifyPlayer,800)}
}
function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n}
function showPlayerCard(res){
  const b=playerBox();b.className='player-status';b.textContent='';
  const card=el('div','pcard');
  const av=el('div','pavatar',(res.name||'?').trim().charAt(0).toUpperCase());av.append(el('i','pdot'));
  const info=el('div','pinfo');
  const top=el('div','ptop');top.append(el('span','pname',res.name),el('span','pbadge','VERIFIED'));
  info.append(top,el('div','pline','Player ID: '+res.uid));
  if(res.region)info.append(el('div','pline','Region: '+res.region));
  const chk=el('div','pcheck');chk.append(el('small',null,'ACCOUNT CHECK'),el('b',null,'100% VALIDATED'),el('span','ptick','\u2713'));
  card.append(av,info,chk);b.append(card);
}
async function verifyPlayer(){
  const uid=document.querySelector('#playerId').value.trim();
  verifiedName='';
  if(!uid){setStatus('','');return}
  setStatus('Verifying player...','wait verifying');
  const same=()=>uid===document.querySelector('#playerId').value.trim();
  try{
    let res=null,r=null;
    for(let i=0;i<2&&!res;i++){
      try{r=await fetch('/api/check-player?id='+encodeURIComponent(uid));res=await readJson(r)}catch(e){res=null}
      if(!same())return;
      if(!res&&i===0){await new Promise(x=>setTimeout(x,2500));if(!same())return}
    }
    if(!res)throw new Error('Name check is unavailable right now. You can still continue.');
    if(!r.ok||!res.success)throw new Error(res.error||'Name check failed');
    verifiedName=res.name;showPlayerCard(res);
  }catch(e){if(!same())return;setStatus(e.message||'Name check failed','bad')}
}
