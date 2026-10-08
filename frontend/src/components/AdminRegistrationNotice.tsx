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
   <section className="welcome-setup"><span className="welcome-source-icon"><Network aria-hidden="true"/></span><div><h3>下一步：配置模型与来源</h3><p>请先配置模型，再添加需要的 arXiv 分类或会议。新增来源默认开启自动抓取和游客展示，您可以随时调整。配置齐全后，可手动运行流水线或等待定时执行。</p><div className="welcome-defaults"><span><i/>自动抓取 · 默认开启</span><span><i/>游客默认 · 默认开启</span></div></div></section>
  </div>
  <footer className="welcome-footer"><button type="button" onClick={onClose}>已保存，稍后配置</button><a href={entry+'/models'}>前往配置模型<ArrowRight aria-hidden="true"/></a></footer>
 </Modal>
}
