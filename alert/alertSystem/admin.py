# This software and any associated files are copyright 2010 Brian Carver and
# Michael Lissner.
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


from django.contrib import admin
from alert.alertSystem.models import *

class CitationAdmin(admin.ModelAdmin):
    # ordering is brutal on MySQL. Don't put it here. Sorry.
    list_display = ('caseNameShort',)
    search_fields = ['caseNameShort', 'caseNameFull', 'caseNumber']
    
class DocumentAdmin(admin.ModelAdmin):
    # ordering is brutal on MySQL. Don't put it here. Sorry.
    list_display = ('citation',)
    list_filter = ('court',)
    search_fields = ['@documentPlainText']


admin.site.register(Court)
admin.site.register(Party)
admin.site.register(Judge)
admin.site.register(JudgeAlias)
admin.site.register(Document, DocumentAdmin)
admin.site.register(Citation, CitationAdmin)
admin.site.register(ExcerptSummary)
