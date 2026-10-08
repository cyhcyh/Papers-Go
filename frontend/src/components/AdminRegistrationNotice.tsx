import {ArrowRight,Copy,ExternalLink,LockKeyhole,Network,ShieldCheck} from 'lucide-react'
import {Modal} from './Common'
import './admin-registration-notice.css'

/** The first administrator's one-time welcome; registration remains with the caller. */
export function AdminRegistrationNotice({entry,onClose,onCopy}:{entry:string;onClose:()=>void;onCopy:()=>void}){
 return <Modal title="管理员注册成功" onClose={onClose} className="admin-registration-notice" titleIcon={<span className="welcome-success-icon"><ShieldCheck aria-hidden="true"/></span>}>
  <p className="welcome-subtitle">保存后台入口，开始配置您的论文站点。</p>
  <div className="welcome-body">
   <div className="welcome-entry-heading"><h3><ShieldCheck aria-hidden="true"/>管理员安全入口</h3><a href={entry} target="_blank" rel="noopener noreferrer">打开管理后台<ExternalLink aria-hidden="true"/></a></div>
   <div className="welcome-entry-address"><code>{window.location.origin+entry}</code><button type="button" onClick={onCopy}><Copy aria-hidden="true"/>复制</button></div>
   <p className="welcome-entry-hint"><LockKeyhole aria-hidden="true"/><span>请复制或收藏此地址。本次提醒关闭后，仅可在后台「基本设置」查看和修改。</span></p>
   <section className="welcome-setup"><span className="welcome-source-icon"><Network aria-hidden="true"/></span><div><h3>下一步：配置分类与来源</h3><p>新安装的来源列表为空。请先添加需要的 arXiv 分类或会议，再选择自动抓取和游客默认展示的来源。</p><div className="welcome-defaults"><span><i/>自动抓取 · 默认关闭</span><span><i/>游客默认 · 默认关闭</span></div></div></section>
  </div>
  <footer className="welcome-footer"><button type="button" onClick={onClose}>已保存，稍后配置</button><a href={entry+'/categories'}>前往配置来源<ArrowRight aria-hidden="true"/></a></footer>
 </Modal>
}
