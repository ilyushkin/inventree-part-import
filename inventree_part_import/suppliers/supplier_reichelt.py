import re

from bs4 import BeautifulSoup
from requests.compat import urljoin
from urllib.parse import quote

from ..error_helper import *
from ..localization import get_language
from .base import ApiPart, ScrapeSupplier, SupplierSupportLevel

BASE_URL = "https://www.reichelt.com/"

class Reichelt(ScrapeSupplier):
    SUPPORT_LEVEL = SupplierSupportLevel.SCRAPING

    def setup(self, language, location, scraping, interactive_part_matches, browser_cookies=""):
        if location not in LOCATION_MAP:
            return self.load_error(f"unsupported location '{location}'")

        if not get_language(language):
            return self.load_error(f"invalid language code '{language}'")

        if not scraping:
            error(f"failed to load '{self.name}' module (scraping is disabled)")
            return False

        self.language = language
        self.location = location

        # Add Accept-Language header matching Netherlands/English preference
        if not hasattr(self, 'extra_headers'):
            self.extra_headers = {}
        
        # Set Accept-Language to prefer English but with Dutch fallback for Netherlands
        self.extra_headers.update({
            'Accept-Language': 'en-US,en;q=0.9,nl;q=0.8,de;q=0.1'
        })

        if browser_cookies:
            self.cookies_from_browser(browser_cookies, "reichelt.com")

        self.max_results = interactive_part_matches

        return True

    def search(self, search_term):
        # Use the actual search URL structure from the website: /nl/en/shop/search
        search_safe = quote(search_term)
        
        # Build search URL matching actual website structure seen in HTML
        # The website uses /nl/en/shop/search with search parameter
        search_url = f"{BASE_URL}nl/en/shop/search?search={search_safe}"
            
        if not (result := self.scrape(search_url)):
            return [], 0

        search_soup = BeautifulSoup(result.content, "html.parser")

        api_parts = []
        search_results = search_soup.find_all("div", class_="al_gallery_article")
        for result in search_results[:self.max_results]:
            # Extract part number, name, and URL using meta tags like the alternative library
            part_meta = result.find("meta", itemprop="productID")
            name_meta = result.find("meta", itemprop="name")
            url_link = result.find("a", class_="al_artinfo_link")
            
            if not part_meta or not name_meta or not url_link:
                continue
                
            sku = part_meta.get("content")
            
            # Extract product URL and ensure it uses Netherlands/English structure
            product_url = url_link.get("href")
            
            # Convert product URL to use Netherlands/English locale structure (/nl/en/)
            full_product_url = urljoin(BASE_URL, product_url)
            
            # Ensure URL uses the correct locale structure - convert to /nl/en/ if needed
            if "/nl/en/" not in full_product_url:
                # Replace any existing locale part with /nl/en/
                # Pattern to match existing locale structures like /de/de/ or /en/en/
                locale_pattern = r'reichelt\.com/[a-z]{2}/[a-z]{2}/'
                if re.search(locale_pattern, full_product_url):
                    full_product_url = re.sub(locale_pattern, 'reichelt.com/nl/en/', full_product_url)
                else:
                    # If no locale structure, add it after domain
                    full_product_url = full_product_url.replace('reichelt.com/', 'reichelt.com/nl/en/')
            
            if not (product_page := self.scrape(full_product_url)):
                continue

            product_page_soup = BeautifulSoup(product_page.content, "html.parser")
            api_part = self.get_api_part(product_page_soup, sku, full_product_url)

            # Only filter if search term doesn't match either SKU or MPN
            if (len(search_results) > 1 and 
                search_term.lower() not in api_part.MPN.lower() and 
                search_term.lower() not in api_part.SKU.lower()):
                continue

            api_parts.append(api_part)

        exact_matches = [
            api_part for api_part in api_parts
            if api_part.SKU.lower() == search_term.lower()
            or api_part.MPN.lower() == search_term.lower()
        ]
        if len(exact_matches) == 1:
            return [exact_matches[0]], 1

        n_results = len(search_results)
        return api_parts, n_results if n_results > self.max_results else len(api_parts)

    def get_api_part(self, soup, sku, link):
        # Get part name using the correct selector
        name_elem = soup.find("h1", attrs={"itemprop":"name"})
        description = name_elem.text.strip() if name_elem else ""

        # Get image URL - try to find product images
        image_url = None
        # Try different possible image selectors
        for img_selector in [
            "img[itemprop='image']",
            ".productImage img",
            "#av_bildbox img",
            ".al_gallery_article img"
        ]:
            img_elem = soup.select_one(img_selector)
            if img_elem and img_elem.get("src"):
                src = img_elem["src"]
                if src.startswith("http"):
                    image_url = src
                else:
                    image_url = urljoin(BASE_URL, src)
                break

        # Get datasheet URL - prioritize actual datasheets over other documents
        datasheet_url = None
        datasheet_divs = soup.find_all("div", attrs={"class": "articleDatasheet"})
        
        # First, look for a datasheet specifically labeled as "Datenblatt" or "Datasheet"
        for div in datasheet_divs:
            datasheet_link = div.find("a")
            if datasheet_link:
                link_text = datasheet_link.get_text(strip=True).lower()
                if "datenblatt" in link_text or "datasheet" in link_text:
                    datasheet_url = urljoin(BASE_URL, datasheet_link.get("href"))
                    break
        
        # If no specific datasheet found, look for PDFs that contain the part number or model
        if not datasheet_url:
            for div in datasheet_divs:
                datasheet_link = div.find("a")
                if datasheet_link:
                    href = datasheet_link.get("href", "")
                    link_text = datasheet_link.get_text(strip=True)
                    # Prefer PDFs that contain the part number/model and are not "replacing" or version-specific docs
                    if (href.endswith(".pdf") and 
                        not "replacing" in link_text.lower() and 
                        not "10xx" in link_text.lower() and
                        len(link_text) > 5):  # Avoid very short generic names
                        datasheet_url = urljoin(BASE_URL, href)
                        break
        
        # Fallback to first available PDF if nothing better found
        if not datasheet_url and datasheet_divs:
            datasheet_link = datasheet_divs[0].find("a")
            if datasheet_link and datasheet_link.get("href", "").endswith(".pdf"):
                datasheet_url = urljoin(BASE_URL, datasheet_link.get("href"))

        # Get availability using the link selector from alternative library
        availability_link = soup.find("link", attrs={"itemprop":"availability"})
        availability = "status_1"  # Default to available
        if availability_link:
            availability_href = availability_link.get("href", "")
            availability_status = availability_href.split("/")[-1] if availability_href else ""
            # Map to the existing availability system
            if "InStock" in availability_status:
                availability = "InStock"
            elif "OutOfStock" in availability_status:
                availability = "OutOfStock"
            elif "PreOrder" in availability_status:
                availability = "PreOrder"
            elif "BackOrder" in availability_status:
                availability = "BackOrder"

        # Get categories from breadcrumb
        category_path = []
        breadcrumb = soup.find_all("ol", class_="breadcrumb")
        if breadcrumb:
            for category in breadcrumb[0].find_all("span", itemprop="name"):
                if category.contents:
                    category_path.append(category.contents[0].strip())

        # Get technical parameters 
        parameters = {}
        data_sections = soup.find_all("ul", attrs={"class":"articleTechnicalData"})
        for data_section in data_sections:
            headline_elem = data_section.find("li", class_="articleTechnicalHeadline")
            if not headline_elem:
                continue
                
            headline = headline_elem.text.strip()
            for attr_section in data_section.find_all("ul", class_="articleAttribute"):
                data_lis = attr_section.find_all("li")
                for i in range(0, len(data_lis), 2):
                    if i + 1 < len(data_lis):
                        name = data_lis[i].text.strip()
                        value = data_lis[i+1].text.strip()
                        # Use flat structure for parameters
                        parameters[name] = value

        # Get manufacturer - try multiple approaches
        manufacturer = "FREI"  # default fallback
        
        # Method 1: Look for itemprop="brand" in manufacturer specifications
        brand_elem = soup.find("li", attrs={"itemprop": "brand"})
        if brand_elem:
            manufacturer = brand_elem.text.strip()
        else:
            # Method 2: Get from parameters if available
            manufacturer = parameters.get("Manufacturer", "FREI")
        
        # Get MPN - try multiple approaches to find the manufacturer part number
        mpn = ""  # Start with empty, fallback to SKU if no MPN found
        
        # Method 1: Look for itemprop="mpn" in manufacturer specifications
        mpn_elem = soup.find("li", attrs={"itemprop": "mpn"})
        if mpn_elem and mpn_elem.text.strip():
            mpn = mpn_elem.text.strip()
        
        if not mpn:
            # Method 2: Look for "Man. part no.:" in the product info section
            man_part_elements = soup.find_all("small")
            for element in man_part_elements:
                text = element.get_text()
                if "Man. part no.:" in text:
                    # Extract the part number from the span with b tag
                    b_elem = element.find("b")
                    if b_elem and b_elem.text.strip():
                        mpn = b_elem.text.strip()
                        break
        
        if not mpn:
            # Method 3: Fallback to parameters if available
            param_mpn = parameters.get("Manufacturer ID", parameters.get("Factory number", ""))
            if param_mpn and param_mpn.strip():
                mpn = param_mpn
        
        # If still no MPN found, use SKU
        if not mpn:
            mpn = sku

        # Get pricing information
        price_breaks = {}
        currency = None
        
        # Get base price and currency
        price_meta = soup.find("meta", itemprop="price")
        currency_meta = soup.find("meta", itemprop="priceCurrency")
        
        if price_meta:
            try:
                price_breaks[1] = float(price_meta.get("content"))
            except (ValueError, TypeError):
                pass
                
        if currency_meta:
            currency = currency_meta.get("content")

        # Try to get discount pricing using the alternative library's approach
        discount_ul = soup.find("ul", class_="discounts")
        if discount_ul:
            for discount_li in discount_ul.find_all("li"):
                span_quant = discount_li.find("span", attrs={"data-discquant": True})
                span_price = discount_li.find("span", attrs={"data-discprice": True})
                
                if span_quant and span_price:
                    try:
                        quantity = int(span_quant.get("data-discquant"))
                        price = float(span_price.get("data-discprice"))
                        price_breaks[quantity] = price
                    except (ValueError, TypeError):
                        continue

        # Fallback: try productPrice elements if discount parsing failed
        if len(price_breaks) <= 1:
            price_elements = soup.find_all('p', class_='productPrice right')
            if price_elements:
                for i, elem in enumerate(price_elements):
                    price_text = elem.get_text(strip=True)
                    # Remove currency symbols and convert to float
                    price_cleaned = re.sub(r'[€$£,]', '', price_text)
                    try:
                        price_val = float(price_cleaned)
                        quantity = 10 ** i if i > 0 else 1
                        price_breaks[quantity] = price_val
                    except ValueError:
                        continue

        return ApiPart(
            description=description,
            image_url=image_url,
            datasheet_url=datasheet_url,
            supplier_link=link,
            SKU=sku.upper(),
            manufacturer=manufacturer,
            manufacturer_link="",
            MPN=mpn,
            quantity_available=AVAILABILITY_MAP.get(availability),
            packaging="",
            category_path=category_path,
            parameters=parameters,
            price_breaks=price_breaks,
            currency=currency,
        )

    def setup_hook(self):
        # Based on actual Reichelt website behavior - use Netherlands/English combination for EUR pricing
        # The site uses URL structure: reichelt.com/nl/en/ for Netherlands country with English language
        if hasattr(self, 'session'):
            # Set cookies to match the actual website behavior observed in HTML
            self.session.cookies.set('LANGUAGE', 'EN', domain='.reichelt.com')  # Uppercase as shown in usersettings
            self.session.cookies.set('LA', '3', domain='.reichelt.com')  # 3 = English language code
            
            # Use Netherlands country code (662) for EUR pricing - this matches LOCATION_MAP['NL']
            netherlands_country_code = LOCATION_MAP['NL']  # 662
            self.session.cookies.set('CCOUNTRY', str(netherlands_country_code), domain='.reichelt.com')
            
            # Additional cookies that might be needed based on website behavior
            self.session.cookies.set('country', 'NL', domain='.reichelt.com')
            
            # Make initial request to Netherlands/English homepage to establish proper session
            try:
                # Use the actual URL structure seen in the HTML: /nl/en/
                establish_url = f"{BASE_URL}nl/en/"
                self.session.get(establish_url, timeout=self.request_timeout)
            except Exception:
                # If this fails, continue anyway
                pass

# None -> available, 0 -> not available
AVAILABILITY_MAP = {
    "status_1": None,
    "status_2": 0,
    "status_3": None,
    "status_4": None,
    "status_5": 0,
    "status_6": 0,
    "status_7": None,
    "status_8": 0,
    # Add support for schema.org availability detection
    "InStock": None,
    "OutOfStock": 0,
    "PreOrder": None,
    "BackOrder": None,
}

LOCATION_MAP = {
    "AT": 458,
    "FR": 443,
    "DE": 445,
    "IT": 446,
    "NL": 662,
    "PL": 470,
    "CH": 459,
    "US": 550,
}
