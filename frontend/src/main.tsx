import 'katex/dist/katex.min.css'
import React from 'react'
import ReactDOM from 'react-dom/client'
import {BrowserRouter} from 'react-router-dom'
import {MotionConfig} from 'framer-motion'
import App from './App'
import {SiteProvider} from './site'
import './styles.css'
import './layouts.css'
import './cards.css'
import './page-content.css'
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><BrowserRouter><MotionConfig reducedMotion="user"><SiteProvider><App/></SiteProvider></MotionConfig></BrowserRouter></React.StrictMode>)
if('serviceWorker' in navigator && import.meta.env.PROD)window.addEventListener('load',()=>{navigator.serviceWorker.register('/sw.js').catch(()=>{})})
