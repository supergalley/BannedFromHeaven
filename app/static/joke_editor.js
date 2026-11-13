/* Progressive enhancement: ordinary textarea remains usable if the editor cannot load. */
(() => {
  'use strict';
  function start(){
    if(!window.Quill)return;
    document.querySelectorAll('textarea[data-joke-editor]').forEach(textarea=>{
      const form=textarea.form,field=form?.querySelector('input[name="body_format"]');
      if(!form||!field)return;
      // Keep legacy staff HTML source intact; the new editor stores text, never arbitrary HTML.
      if(!field.value&&textarea.dataset.legacyHtml==='1'&&/<\/?[a-z][^>]*>/i.test(textarea.value))return;
      const wrap=document.createElement('div');wrap.className='joke-editor-wrap';
      const surface=document.createElement('div');wrap.append(surface);textarea.before(wrap);
      try{
        const quill=new Quill(surface,{theme:'snow',formats:['bold','italic','strike'],
          placeholder:textarea.placeholder||'Write your joke…',
          modules:{toolbar:[['bold','italic','strike'],['clean']],history:{userOnly:true}}});
        let saved=null;try{saved=JSON.parse(field.value||'null');}catch(_error){}
        if(saved?.ops&&saved.ops.every(op=>typeof op.insert==='string')&&saved.ops.map(op=>op.insert).join('').trim()===textarea.value.trim())quill.setContents(saved,'silent');
        else quill.setText(textarea.value,'silent');
        quill.root.setAttribute('role','textbox');quill.root.setAttribute('aria-multiline','true');
        quill.root.setAttribute('aria-label',textarea.dataset.editorLabel||'Joke or caption');
        quill.root.spellcheck=true;
        const labels={bold:'Bold (Ctrl+B)',italic:'Italic (Ctrl+I)',strike:'Strikethrough',clean:'Clear formatting'};
        const toolbar=wrap.querySelector('.ql-toolbar');toolbar.setAttribute('role','toolbar');toolbar.setAttribute('aria-label','Text formatting');
        toolbar.querySelectorAll('button').forEach(button=>{const name=[...button.classList].find(c=>c.startsWith('ql-'))?.slice(3);button.type='button';button.title=labels[name]||name;button.setAttribute('aria-label',labels[name]||name);});
        let plainMode=false;
        const sync=()=>{if(plainMode)return;textarea.value=quill.getText().replace(/\n$/,'');field.value=JSON.stringify(quill.getContents());};
        sync();field.disabled=false;textarea.hidden=true;
        if(textarea.dataset.legacyHtml==='1'){
          const source=document.createElement('button');source.type='button';source.textContent='HTML';source.title='Switch to staff HTML source';source.setAttribute('aria-label',source.title);
          source.addEventListener('click',()=>{textarea.value=quill.getSemanticHTML();plainMode=true;field.disabled=true;wrap.hidden=true;textarea.hidden=false;textarea.focus();});toolbar.append(source);
        }
        quill.on('text-change',()=>{sync();textarea.dispatchEvent(new Event('input',{bubbles:true}));});
        form.addEventListener('submit',sync,true);
        form.addEventListener('formdata',event=>{sync();event.formData.set('body',textarea.value);if(!plainMode)event.formData.set('body_format',field.value);else event.formData.delete('body_format');});
        form.addEventListener('reset',()=>setTimeout(()=>{if(plainMode)return;if(saved?.ops)quill.setContents(saved);else quill.setText(textarea.defaultValue);sync();},0));
        // Image pastes bubble to the existing meme upload handler; no embedded images in text.
        textarea.jokeEditor=quill;
      }catch(error){wrap.remove();textarea.hidden=false;field.disabled=true;console.warn('Text editor unavailable; using plain text.',error);}
    });
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start);else start();
})();
