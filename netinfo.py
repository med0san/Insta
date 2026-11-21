"""Affiche les adresses à saisir dans l'application Android (appelé par run-mobile.bat)."""
import access

found = access.addresses()
print()
if found["tailscale"]:
    for ip in found["tailscale"]:
        print(f"  TELEPHONE EN 4G / HORS WI-FI (Tailscale) : {ip}:8000")
else:
    print("  Tailscale non detecte sur ce PC (necessaire si le telephone est en 4G : voir README).")
for ip in found["lan"]:
    print(f"  Telephone sur le MEME Wi-Fi que le PC     : {ip}:8000")
print("  (N'utilisez JAMAIS 0.0.0.0 : ce n'est pas une adresse, c'est le mode d'ecoute du serveur.)")
print()
