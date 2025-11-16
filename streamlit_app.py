# app.py
import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import io
import math

from gspread import authorize
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="Distribuidora 2L — ABC & Políticas", layout="wide")

# -------------------------
#  Helper: connect to sheet
# -------------------------
@st.cache_data(ttl=300)
def load_sheet(spreadsheet_name: str, worksheet_name: str):
    """
    Load sheet into DataFrame using service account credentials stored in st.secrets.
    NOTE: In Streamlit Cloud, add the entire service-account JSON to Secrets as key:
          gcp_service_account
    """
    # get credentials from streamlit secrets as dict
    info = st.secrets["gcp_service_account"]  # must be a parsed JSON object in Secrets
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    client = authorize(creds)

    sh = client.open(spreadsheet_name)
    ws = sh.worksheet(worksheet_name)
    data = ws.get_all_values()
    df = pd.DataFrame(data[1:], columns=data[0])
    return df

# -------------------------
#  Calculations
# -------------------------
def prepare_df(df_raw, day_prefix="Dia_"):
    # detect day columns automatically
    day_cols = [c for c in df_raw.columns if c.startswith(day_prefix)]
    # convert numeric sales columns
    for c in day_cols:
        df_raw[c] = pd.to_numeric(df_raw[c].str.replace(",", "").str.replace(" ", ""), errors="coerce").fillna(0)
    # numeric for other useful cols (if exist)
    for col in ["Costo_unitario","Dinero_Ventas","Unidades_Total","Costo_Total","d_Promedio","Variacion_D","Lead_Time","Stock_actual"]:
        if col in df_raw.columns:
            df_raw[col] = pd.to_numeric(df_raw[col].astype(str).str.replace(",", "").str.replace(" ", ""), errors="coerce')
    # compute monthly total from day cols if not present
    if "total_mes" not in df_raw.columns:
        df_raw["total_mes"] = df_raw[day_cols].sum(axis=1)
    # demand average daily (if not present)
    if "d_Promedio" not in df_raw.columns:
        df_raw["d_Promedio"] = (df_raw[day_cols].mean(axis=1)).fillna(0)
    # variation, sigma
    if "Variacion_D" not in df_raw.columns:
        df_raw["Variacion_D"] = df_raw[day_cols].std(axis=1).fillna(0)
    # Lead time default
    if "Lead_Time" not in df_raw.columns:
        df_raw["Lead_Time"] = 3
    # Stock actual default (optional)
    if "Stock_actual" not in df_raw.columns:
        df_raw["Stock_actual"] = np.nan

    return df_raw, day_cols

def classify_abc(df, value_col="total_mes", a_pct=0.80, b_pct=0.95):
    df_sorted = df.sort_values(value_col, ascending=False).reset_index(drop=True).copy()
    df_sorted["pct"] = df_sorted[value_col] / max(df_sorted[value_col].sum(), 1)
    df_sorted["pct_acum"] = df_sorted["pct"].cumsum()
    def lab(x):
        if x <= a_pct: return "A"
        elif x <= b_pct: return "B"
        else: return "C"
    df_sorted["Clase_ABC"] = df_sorted["pct_acum"].apply(lab)
    return df_sorted

def compute_policies(df, ordering_cost=30000.0, holding_rate=0.20, service_level_A=0.98, service_level_B=0.95, review_T_B=5):
    # z-values
    from scipy.stats import norm
    zA = norm.ppf(service_level_A)
    zB = norm.ppf(service_level_B)

    rows = []
    for _, r in df.iterrows():
        product = r.get("codigo", "")
        name = r.get("nombre", "")
        abc = r.get("Clase_ABC", "C")
        d = float(r.get("d_Promedio", 0))          # demanda diaria promedio
        sigma = float(r.get("Variacion_D", 0))     # desviación diaria
        LT = float(r.get("Lead_Time", 3))
        stock = r.get("Stock_actual", np.nan)
        cost = float(r.get("Costo_unitario", 0.0))
        # annualize demand and holding cost
        D_annual = d * 365
        h = holding_rate * cost

        entry = r.copy()
        # default fields
        entry["Policy"] = ""
        entry["Q_or_S"] = np.nan
        entry["ROP"] = np.nan
        entry["SS"] = np.nan
        entry["Order_if_review"] = np.nan
        entry["Alert"] = ""

        if abc == "A":
            # Continuous Q-R: EOQ for Q, ROP and SS
            if h>0 and D_annual>0:
                Q = math.sqrt((2 * D_annual * ordering_cost) / h)
            else:
                Q = max(1, d*30)  # fallback
            demand_during_LT = d * LT
            sigma_LT = sigma * math.sqrt(max(1, LT))
            ss = zA * sigma_LT
            ROP = math.ceil(demand_during_LT + ss)

            entry["Policy"] = "Revisión Continua (Q)"
            entry["Q_or_S"] = int(round(Q))
            entry["ROP"] = int(ROP)
            entry["SS"] = int(math.ceil(ss))

            # Alert: if stock known and <= ROP then order now
            try:
                if not np.isnan(stock) and float(stock) <= ROP:
                    entry["Alert"] = "PEDIR AHORA (stock <= ROP)"
                else:
                    entry["Alert"] = "OK"
            except:
                entry["Alert"] = "SIN STOCK REA"
        elif abc == "B":
            # Periodic review: T days (user-defined) -> S level
            T = review_T_B
            demand_during = d * (LT + T)
            sigma_d = sigma * math.sqrt(max(1, LT + T))
            ss = zB * sigma_d
            S = math.ceil(demand_during + ss)
            entry["Policy"] = f"Revisión Periódica (T={T}d)"
            entry["Q_or_S"] = int(S)
            entry["SS"] = int(math.ceil(ss))
            # order if S - stock >0 (if stock known)
            try:
                if np.isnan(stock):
                    entry["Order_if_review"] = np.nan
                    entry["Alert"] = "SIN STOCK ACTUAL"
                else:
                    order_qty = max(0, S - float(stock))
                    entry["Order_if_review"] = int(order_qty)
                    entry["Alert"] = "PEDIR EN REVISIÓN" if order_qty>0 else "NO PEDIR"
            except:
                entry["Alert"] = "ERROR STOCK"
        else:
            # C - Min-Max simple
            entry["Policy"] = "Min-Max (baja prioridad)"
            entry["Q_or_S"] = int(max(1, d * 7))  # suggested reorder qty (max)
            entry["ROP"] = int(max(0, d * 3))
            entry["Alert"] = "NO APLICADO (C)"

        rows.append(entry)

    df_out = pd.DataFrame(rows)
    return df_out

# -------------------------
#  UI - sidebar (settings)
# -------------------------
st.sidebar.title("Configuración")
sheet_name = st.sidebar.text_input("Nombre Google Sheet", "VENTAS_MES_12_INVENTARIOS_COMPLETO")
worksheet = st.sidebar.text_input("Nombre worksheet/tab", "Sheet1")
day_prefix = st.sidebar.text_input("Prefijo columnas día", "Dia_")
ordering_cost = st.sidebar.number_input("Costo por orden (K)", value=30000.0, step=1000.0)
holding_rate = st.sidebar.number_input("Tasa de mantenimiento (anual %) ", value=0.20, step=0.01, format="%.2f")
service_A = st.sidebar.number_input("Nivel de servicio A (ej 0.98)", value=0.98, format="%.2f")
service_B = st.sidebar.number_input("Nivel de servicio B (ej 0.95)", value=0.95, format="%.2f")
review_T_B = st.sidebar.number_input("Periodo revisión B (días)", value=5, step=1)

st.sidebar.markdown("---")
st.sidebar.markdown("**Credenciales**")
st.sidebar.info("En Streamlit Cloud debes colocar la JSON de la cuenta de servicio en Secrets (clave: gcp_service_account).")

# -------------------------
#  Main
# -------------------------
st.title("📦 Distribuidora 2L — ABC & Políticas")
st.caption("Dashboard para generar políticas automáticas según Clase ABC. Fuente: Google Sheets")

with st.spinner("Cargando datos desde Google Sheets..."):
    try:
        df_raw = load_sheet(sheet_name, worksheet)
    except Exception as e:
        st.error("Error cargando Google Sheet. Revisa el nombre y las credenciales en Secrets.")
        st.exception(e)
        st.stop()

# prepare
df_prep, day_cols = prepare_df(df_raw.copy(), day_prefix=day_prefix)
df_abc = classify_abc(df_prep, value_col="total_mes")

# compute policies
df_pols = compute_policies(df_abc, ordering_cost=ordering_cost, holding_rate=holding_rate,
                           service_level_A=service_A, service_level_B=service_B, review_T_B=review_T_B)

# Top KPIs
col1, col2, col3, col4 = st.columns(4)
total_value = df_abc["total_mes"].sum()
nA = (df_abc["Clase_ABC"]=="A").sum()
nB = (df_abc["Clase_ABC"]=="B").sum()
nC = (df_abc["Clase_ABC"]=="C").sum()
col1.metric("Total unidades (mes)", f"{int(total_value):,}")
col2.metric("Productos A", nA)
col3.metric("Productos B", nB)
col4.metric("Productos C", nC)

st.markdown("### 📊 Gráficos")

# Pareto: top products by total_mes
fig_pareto = px.bar(df_abc.head(30), x="nombre", y="total_mes", color="Clase_ABC",
                    title="Top 30 productos por ventas (mes)", labels={"total_mes":"Ventas (u)"})
fig_pareto.update_layout(xaxis_tickangle=-45)
st.plotly_chart(fig_pareto, use_container_width=True)

# Pie ABC by sales value
if "Dinero_Ventas" in df_prep.columns:
    abc_val = df_prep.groupby("Clase_ABC")["Dinero_Ventas"].sum().reset_index()
    fig_pie = px.pie(abc_val, names="Clase_ABC", values="Dinero_Ventas", title="Participación por Clase (valor ventas)")
    st.plotly_chart(fig_pie, use_container_width=True)

st.markdown("### ⚙️ Tabla de Políticas sugeridas")
# show table with useful columns
show_cols = ["codigo", "nombre", "Clase_ABC", "total_mes", "d_Promedio", "Variacion_D", "Lead_Time",
             "Policy", "Q_or_S", "ROP", "SS", "Order_if_review", "Alert"]
# ensure columns exist
for c in show_cols:
    if c not in df_pols.columns:
        df_pols[c] = np.nan

st.dataframe(df_pols[show_cols].fillna("").sort_values(["Clase_ABC","total_mes"], ascending=[True, False]), height=500)

# Download excel
def to_excel_bytes(df_export):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df_export.to_excel(writer, index=False, sheet_name="Politicas")
    return output.getvalue()

excel_bytes = to_excel_bytes(df_pols)
st.download_button("⬇️ Descargar parámetros (Excel)", data=excel_bytes, file_name="parametros_inventario_Distribuidora2L.xlsx")

st.success("Listo — puedes revisar la tabla y descargar el Excel. Para desplegar públicamente, sigue las instrucciones en el README.")
