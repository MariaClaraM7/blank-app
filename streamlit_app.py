import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import gspread
from google.oauth2.service_account import Credentials

# ------------------------------
# CONFIGURACIÓN GENERAL
# ------------------------------
st.set_page_config(
    page_title="Dashboard Inventarios ABC",
    layout="wide"
)

st.title("📦 Dashboard de Inventarios – Clasificación ABC + Políticas")

st.write("""
Este panel conecta automáticamente con Google Sheets, clasifica los productos (A/B/C)
y genera las políticas de inventario para cada tipo.
""")

# ------------------------------
# CONEXIÓN GOOGLE SHEETS
# ------------------------------

st.sidebar.header("🔐 Conexión Google Sheets")

gsheet_url = st.sidebar.text_input(
    "URL de la Google Sheet",
    placeholder="Pega aquí la URL completa del documento"
)

json_file = st.sidebar.file_uploader(
    "Sube tu archivo credentials.json",
    type=["json"]
)

worksheet_name = st.sidebar.text_input(
    "Nombre de la hoja",
    "Sheet1"
)

load_button = st.sidebar.button("📥 Cargar Datos")

if load_button:

    try:
        # Cargar credenciales
        creds = Credentials.from_service_account_info(
            json_file.read(),
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )

        gc = gspread.authorize(creds)

        # Abrir Sheet
        sh = gc.open_by_url(gsheet_url)
        ws = sh.worksheet(worksheet_name)

        raw = ws.get_all_values()
        df_raw = pd.DataFrame(raw[1:], columns=raw[0])

        st.success("Datos cargados correctamente.")

        # ------------------------------
        # LIMPIEZA DE VARIABLES
        # ------------------------------
        numeric_cols = [
            "Costo_unitario",
            "Dinero_Ventas",
            "Unidades_Total",
            "Costo_Total",
            "d_Promedio",
            "Variacion_D",
            "Lead_Time",
            "Stock_actual"
        ]

        for col in numeric_cols:
            if col in df_raw.columns:
                df_raw[col] = pd.to_numeric(
                    df_raw[col].astype(str).str.replace(",", "").str.replace(" ", ""),
                    errors="coerce"
                )

        # ------------------------------
        # CLASIFICACIÓN ABC
        # ------------------------------
        df_raw["Valor_anual"] = df_raw["Dinero_Ventas"]

        df = df_raw.sort_values("Valor_anual", ascending=False)
        df["%"] = df["Valor_anual"] / df["Valor_anual"].sum()
        df["%_acum"] = df["%"].cumsum()

        df["ABC"] = np.where(
            df["%_acum"] <= 0.80, "A",
            np.where(df["%_acum"] <= 0.95, "B", "C")
        )

        # ------------------------------
        # POLÍTICAS DE INVENTARIO
        # ------------------------------

        # Parámetros tipo A (Q)
        Z_A = 1.65
        L_A = 2  # días

        # Parámetros tipo B (P)
        Z_B = 1.30
        L_B = 5
        T_B = 5  # periodo de revisión

        d_std = df["d_Promedio"].std()

        def calc_politica(row):
            d = row["d_Promedio"]
            if row["ABC"] == "A":
                # Revisión Continua (Q)
                R = d * L_A + Z_A * d_std
                return f"Q | R = {R:.1f}"
            elif row["ABC"] == "B":
                # Revisión Periódica (P)
                S = d * (L_B + T_B) + Z_B * d_std
                return f"P | S = {S:.1f}"
            else:
                return "Sin política (C)"

        df["Política"] = df.apply(calc_politica, axis=1)

        # ------------------------------
        # DASHBOARD
        # ------------------------------

        st.subheader("📊 Clasificación ABC – Valor Anual")

        fig_abc = px.bar(
            df,
            x="Producto",
            y="Valor_anual",
            color="ABC",
            title="Clasificación ABC por Valor Anual"
        )

        st.plotly_chart(fig_abc, use_container_width=True)

        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Distribución ABC")
            fig_dist = px.histogram(df, x="ABC", color="ABC")
            st.plotly_chart(fig_dist, use_container_width=True)

        with col2:
            st.subheader("Políticas generadas")
            fig_pol = px.histogram(df, x="Política", color="ABC")
            st.plotly_chart(fig_pol, use_container_width=True)

        st.subheader("📄 Tabla completa de productos")
        st.dataframe(df, use_container_width=True)

    except Exception as e:
        st.error(f"Error cargando datos: {e}")
