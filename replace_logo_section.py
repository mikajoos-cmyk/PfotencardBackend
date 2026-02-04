import re

# Read the file with latin-1 encoding to handle special characters
with open(r'c:\Users\y.penazzi\Documents\Mika\Pfotencard\pfotencard-marketing-website\src\pages\EinstellungenPage.tsx', 'r', encoding='latin-1') as f:
    content = f.read()

# Define the old section to replace
old_section = '''                                 <div className="space-y-2">
                                  <Label>Rechnungs-Logo URL (Optional)</Label>
                                  <div className="flex gap-2">
                                    <Input value={invoiceSettings.logo_url} onChange={e => setInvoiceSettings({ ...invoiceSettings, logo_url: e.target.value })} placeholder="https://..." />
                                  </div>
                                  <p className="text-xs text-muted-foreground">Wenn leer, wird das Logo aus den Design-Einstellungen verwendet.</p>
                                </div>'''

# Define the new section
new_section = '''                                 <div className="space-y-2 py-2">
                                  <Label>Rechnungs-Logo (Optional)</Label>
                                  <p className="text-xs text-muted-foreground mb-2">Falls leer, wird dein normales Schul-Logo verwendet.</p>
                                  <input type="file" id="invoice-logo-upload-input" className="hidden" accept="image/*" onChange={handleInvoiceLogoFileChange} />
                                  <div 
                                    onClick={handleInvoiceLogoUpload}
                                    className={`relative border-2 border-dashed rounded-lg p-4 text-center cursor-pointer transition-colors ${invoiceSettings.logo_url ? 'border-primary bg-primary/5' : 'border-border hover:border-primary hover:bg-muted'}`}
                                  >
                                    {invoiceSettings.logo_url ? (
                                      <div className="flex items-center justify-center gap-4">
                                        <div className="w-16 h-16 bg-white rounded border flex items-center justify-center overflow-hidden">
                                          <img src={invoiceSettings.logo_url} alt="Invoice Logo" className="w-full h-full object-contain" />
                                        </div>
                                        <div className="text-left">
                                          <p className="text-sm font-medium">Eigenes Rechnungslogo aktiv</p>
                                          <Button variant="ghost" size="sm" className="h-7 px-2 text-xs mt-1" onClick={(e) => {
                                            e.stopPropagation();
                                            setInvoiceSettings({ ...invoiceSettings, logo_url: '' });
                                          }}>
                                            <Trash2 size={12} className="mr-1" /> Entfernen (Branding-Logo nutzen)
                                          </Button>
                                        </div>
                                      </div>
                                    ) : (
                                      <div className="flex flex-col items-center gap-2">
                                        <Upload size={24} className="text-muted-foreground" />
                                        <p className="text-xs font-medium">Klicke zum Hochladen eines speziellen Rechnungs-Logos</p>
                                      </div>
                                    )}
                                  </div>
                                </div>'''

# Replace
if old_section in content:
    content = content.replace(old_section, new_section)
    print("✓ Replacement successful!")
else:
    print("✗ Old section not found - trying line-by-line approach")
    lines = content.split('\n')
    # Find and replace lines 1787-1793 (0-indexed: 1786-1792)
    if len(lines) > 1793:
        new_lines = lines[:1786] + new_section.split('\n') + lines[1793:]
        content = '\n'.join(new_lines)
        print("✓ Line-based replacement successful!")

# Write back with latin-1 encoding
with open(r'c:\Users\y.penazzi\Documents\Mika\Pfotencard\pfotencard-marketing-website\src\pages\EinstellungenPage.tsx', 'w', encoding='latin-1') as f:
    f.write(content)

print("File updated successfully!")
