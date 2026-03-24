#!/usr/bin/env python3
"""
Teste rápido do follow-up para ver o que está sendo enviado à API.
"""

# Simular o contexto de follow-up
context_parts = [
    "Data de hoje: segunda-feira, 24 de março de 2026 (14:30)",
    "\n--- Contexto da pergunta anterior (grades) ---",
    """Com base nas informações fornecidas, aqui estão as tuas notas organizadas por unidade curricular:

### 1º Semestre
- **AM I**: 12 (ECTS: D) [6 ECTS]
- **FP**: 16 (ECTS: C) [6 ECTS]
- **FSC**: 16 (ECTS: B) [6 ECTS]
- **MD**: 15 (ECTS: B) [6 ECTS]
- **PUP**: 13 (ECTS: E) [1.5 ECTS]
- **ALGA**: 16 (ECTS: B) [4.5 ECTS]
- **AED**: 12 (ECTS: E) [6 ECTS]
- **BD**: 14 (ECTS: B) [6 ECTS]
- **F II**: 15 (ECTS: C) [4.5 ECTS]
- **IADP**: 20 (ECTS: A) [1.5 ECTS]
- **LDTS**: 16 (ECTS: B) [6 ECTS]
- **SO**: 14 (ECTS: C) [6 ECTS]
- **FSI**: 15 (ECTS: B) [6 ECTS]
- **IPC**: 17 (ECTS: B) [4.5 ECTS]
- **IADE**: 20 (ECTS: A) [1.5 ECTS]
- **LBAW**: 16 (ECTS: C) [6 ECTS]
- **PFL**: 11 (ECTS: D) [6 ECTS]
- **RC**: 17 (ECTS: B) [6 ECTS]

### 2º Semestre
- **AM II**: 16 (ECTS: B) [6 ECTS]
- **AC**: 13 (ECTS: C) [6 ECTS]
- **F I**: 17 (ECTS: B) [6 ECTS]
- **P**: 16 (ECTS: C) [6 ECTS]
- **TC**: 15 (ECTS: B) [6 ECTS]
- **CP**: 12 (ECTS: D) [1.5 ECTS]
- **DA**: 16 (ECTS: B) [6 ECTS]
- **ES**: 18 (ECTS: B) [6 ECTS]
- **LC**: 18 (ECTS: B) [6 ECTS]
- **LTW**: 15 (ECTS: B) [6 ECTS]
- **ME**: 17 (ECTS: A) [4.5 ECTS]

Se precisares de mais alguma informação ou análise sobre as tuas notas, avisa! 😊"""
]

question = "e qual delas é a menor?"

enriched_message = f"""{chr(10).join(context_parts)}

Pergunta do utilizador: {question}

Responde com base nos dados acima. Se a informação não estiver disponível, indica isso claramente."""

print(f"Tamanho total da mensagem: {len(enriched_message)} chars")
print(f"\nPrimeiros 500 chars:")
print(enriched_message[:500])
print("\n...")
print(f"\nÚltimos 300 chars:")
print(enriched_message[-300:])
