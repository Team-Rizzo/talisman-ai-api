from apify_client import ApifyClient
from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Dict, Any


class ImageValue(BaseModel):
    height: int
    width: int
    url: str


class ImageColorPalette(BaseModel):
    rgb: Optional[Dict[str, int]] = None
    percentage: float


class ImageColorValue(BaseModel):
    palette: List[ImageColorPalette]


class UserValue(BaseModel):
    id_str: str
    path: List[Any] = []


class BindingValue(BaseModel):
    string_value: Optional[str] = None
    image_value: Optional[ImageValue] = None
    image_color_value: Optional[ImageColorValue] = None
    user_value: Optional[UserValue] = None
    type: str


class CardBindingValues(BaseModel):
    model_config = ConfigDict(extra='allow')
    
    player_url: Optional[BindingValue] = None
    player_image_large: Optional[BindingValue] = None
    player_image: Optional[BindingValue] = None
    app_star_rating: Optional[BindingValue] = None
    description: Optional[BindingValue] = None
    player_width: Optional[BindingValue] = None
    domain: Optional[BindingValue] = None
    app_is_free: Optional[BindingValue] = None
    site: Optional[BindingValue] = None
    player_image_original: Optional[BindingValue] = None
    app_url_resolved: Optional[BindingValue] = None
    app_num_ratings: Optional[BindingValue] = None
    app_price_amount: Optional[BindingValue] = None
    player_height: Optional[BindingValue] = None
    vanity_url: Optional[BindingValue] = None
    app_name: Optional[BindingValue] = None
    app_id: Optional[BindingValue] = None
    player_image_small: Optional[BindingValue] = None
    title: Optional[BindingValue] = None
    app_price_currency: Optional[BindingValue] = None
    app_url: Optional[BindingValue] = None
    card_url: Optional[BindingValue] = None
    player_image_color: Optional[BindingValue] = None
    player_image_x_large: Optional[BindingValue] = None


class Card(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    card_type_url: Optional[str] = None
    binding_values: Optional[CardBindingValues] = None
    users: Optional[Dict[str, Any]] = None


class UserMention(BaseModel):
    id_str: str
    name: str
    screen_name: str
    indices: List[int]


class Url(BaseModel):
    display_url: str
    expanded_url: str
    url: str
    indices: List[int]


class Entities(BaseModel):
    user_mentions: List[UserMention] = []
    urls: List[Url] = []
    hashtags: List[Any] = []
    symbols: List[Any] = []
    media: List[Any] = []


class UserEntities(BaseModel):
    description: Optional[Dict[str, Any]] = None
    url: Optional[Dict[str, Any]] = None


class User(BaseModel):
    blocking: bool = False
    created_at: str
    default_profile: bool = True
    default_profile_image: bool = False
    description: str = ""
    entities: Optional[UserEntities] = None
    fast_followers_count: int = 0
    favourites_count: int = 0
    follow_request_sent: bool = False
    followed_by: bool = False
    followers_count: int = 0
    following: bool = False
    friends_count: int = 0
    has_custom_timelines: bool = False
    id: int = 0
    id_str: str
    is_translator: bool = False
    listed_count: int = 0
    location: str = ""
    media_count: int = 0
    name: str
    normal_followers_count: int = 0
    notifications: bool = False
    profile_banner_url: Optional[str] = None
    profile_image_url_https: Optional[str] = None
    protected: bool = False
    screen_name: str
    show_all_inline_media: bool = False
    statuses_count: int = 0
    time_zone: str = ""
    translator_type: str = "none"
    url: Optional[str] = None
    utc_offset: Optional[int] = None
    verified: bool = False
    verified_type: Optional[str] = None
    withheld_in_countries: List[str] = []
    withheld_scope: str = ""
    is_blue_verified: bool = False


class Tweet(BaseModel):
    id: int = 0
    location: str = ""
    card: Optional[Card] = None
    conversation_id_str: str
    created_at: str
    display_text_range: List[int]
    entities: Entities
    favorite_count: int = 0
    favorited: bool = False
    full_text: str
    id_str: str
    in_reply_to_name: Optional[str] = None
    in_reply_to_screen_name: Optional[str] = None
    in_reply_to_user_id_str: Optional[str] = None
    lang: str
    permalink: str
    possibly_sensitive: bool = False
    quote_count: int = 0
    reply_count: int = 0
    retweet_count: int = 0
    retweeted: bool = False
    text: str
    user: User
    startUrl: Optional[str] = None


class ApifyScraper:
    def __init__(self, token: str):
        self.client = ApifyClient(token)

    
    def scrape_tweet_by_handle(self, handle: str) -> List[Tweet]:
        """
        Scrape a tweet by its handle using the Apify API client.
        Returns a list of Tweet Pydantic objects.
        """
        run_input = {
            "startUrls": [{"url": f"https://x.com/{handle}"}],
        }
        run = self.client.actor("KVJr35xjTw2XyvMeK").call(run_input=run_input)
        
        # Fetch and collect Actor results from the run's dataset
        scraped_items = []
        for item in self.client.dataset(run["defaultDatasetId"]).iterate_items():
            scraped_items.append(item)
        
        # Convert to Pydantic models
        tweets = [Tweet(**item) for item in scraped_items]
        for tweet in tweets:
            tweet.id = tweet.id_str
        return tweets
    