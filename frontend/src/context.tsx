import {createContext,useContext} from 'react'
import type {Auth} from './api'
export const AppContext=createContext<{auth:Auth|null;toast:(text:string)=>void;logout:()=>void;requireLogin:(reason:string,resume?:()=>void)=>boolean}>({auth:null,toast:()=>{},logout:()=>{},requireLogin:()=>false})
export const useApp=()=>useContext(AppContext)
