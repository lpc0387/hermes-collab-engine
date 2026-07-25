from fpdf import FPDF

pdf = FPDF()
pdf.add_page()
pdf.set_font("Helvetica", size=40)
pdf.cell(text="hello world", align="C", new_x="LMARGIN", new_y="NEXT")
pdf.output("output.pdf")
print("PDF generated: output.pdf")
